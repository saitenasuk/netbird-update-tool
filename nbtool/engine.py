"""任务编排：顺序、门禁、前置检查、detach 等待、版本对比。

失败语义（对应原 Ansible 方案）：
  - group_order 里第一个分组（默认 console）任一台失败 → 整个任务立即中止
  - 其余分组某台失败 → 记录后继续下一台
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+[-\w.]*)")


@dataclass
class Settings:
    group_order: list[str] = field(default_factory=lambda: ["console", "clients"])
    script_timeout: int = 1800
    detach_wait_timeout: int = 900
    detach_poll_timeout: int = 600
    detach_connect_interval: int = 10
    detach_poll_interval: int = 10
    connect_retries: int = 2
    connect_timeout: int = 10
    keep_jobs: int = 50


class Engine:
    def __init__(self, transport, settings: Settings | None = None, sleep=time.sleep):
        self.transport = transport
        self.s = settings or Settings()
        self.sleep = sleep

    # ---------- 排序 ----------

    def order(self, hosts: list[dict]) -> list[dict]:
        """console 组先、clients 组后；组内按 sort_order；self_host 永远最后。"""
        order = self.s.group_order

        def rank(host: dict):
            group = host.get("group_name") or "clients"
            gi = order.index(group) if group in order else len(order)
            return (
                gi,
                1 if host.get("self_host") else 0,
                int(host.get("sort_order") or 0),
                int(host["id"]),
            )

        return sorted(hosts, key=rank)

    # ---------- 执行 ----------

    def run(self, targets: list[dict], emit, is_aborted=lambda: False) -> dict:
        results: list[dict] = []
        aborted = False
        total = len(targets)
        emit("", f"任务开始 · {total} 台目标 · 顺序 {' → '.join(self.s.group_order)}", "dim")

        for index, target in enumerate(targets, 1):
            host = target["host"]
            if is_aborted():
                aborted = True
                emit(host["name"], "已中止，剩余机器未执行", "err")
                break
            emit("", f"──── {index}/{total} {host['name']} ────", "dim")
            result = self._run_host(host, target["scripts"], emit, is_aborted)
            results.append(result)
            if result["status"] != "ok":
                first_group = self.s.group_order[0] if self.s.group_order else None
                if first_group and (host.get("group_name") or "") == first_group:
                    emit(
                        host["name"],
                        f"{host['name']} 属于优先分组 {first_group}，任务中止，其余机器未执行",
                        "err",
                    )
                    aborted = True
                    break
            if is_aborted():
                aborted = True
                break

        if aborted:
            status = "aborted"
        elif any(r["status"] != "ok" for r in results):
            status = "failed"
        else:
            status = "success"
        emit("", f"任务结束 · {status} · {len(results)}/{total} 台已处理", "ok" if status == "success" else "err")
        return {"status": status, "hosts": results}

    def _run_host(self, host, scripts, emit, is_aborted) -> dict:
        name = host["name"]
        started = time.monotonic()
        result = {
            "host_id": host.get("id"),
            "name": name,
            "group": host.get("group_name"),
            "status": "ok",
            "error": "",
            "version_before": None,
            "version_after": None,
            "duration": 0.0,
        }

        def fail(message: str) -> dict:
            result["status"] = "failed"
            result["error"] = message
            result["duration"] = round(time.monotonic() - started, 1)
            emit(name, message, "err")
            return result

        # 1. 连通性
        emit(name, "连通性检查", "info")
        reachable = False
        last = None
        for attempt in range(1, self.s.connect_retries + 1):
            last = self.transport.run(host, ["true"], timeout=self.s.connect_timeout + 5)
            if last.ok:
                reachable = True
                break
            if attempt < self.s.connect_retries:
                emit(name, f"第 {attempt} 次连接失败，重试", "dim")
        if not reachable:
            detail = (last.text.strip().splitlines() or ["无输出"])[-1] if last else "无输出"
            return fail(f"SSH 连不上：{detail}")

        # 2. 脚本前置检查：缺一个就整台跳过，不进入更新
        for script in scripts:
            probe = self.transport.run(host, ["test", "-x", script["path"]], timeout=30)
            if not probe.ok:
                return fail(f"脚本不可执行或不存在：{script['path']}（需要 chmod +x）")
        emit(name, f"脚本检查通过（{len(scripts)} 个）", "dim")

        # 3. 更新前版本
        result["version_before"] = self._version(host)
        emit(name, f"当前版本 {result['version_before'] or '未安装'}", "dim")

        # 4. 按顺序执行
        for script in scripts:
            if is_aborted():
                return fail("已中止")
            argv = [script["path"], *script.get("args", [])]
            emit(name, "▸ " + " ".join(argv), "info")
            timeout = script.get("timeout") or self.s.script_timeout

            def relay(line: str, _name=name) -> None:
                if line.strip():
                    emit(_name, "  " + line, "dim")

            run = self.transport.run(
                host, argv, timeout=timeout, on_line=relay, cancel=is_aborted
            )
            if run.cancelled:
                return fail(f"{script['path']} 已中止")
            if run.timed_out:
                return fail(f"{script['path']} 超时（{timeout}s）")
            if run.rc != 0:
                tail = (run.lines[-1].strip() if run.lines else "") or "无输出"
                return fail(f"{script['path']} 退出码 {run.rc}：{tail}")
            emit(name, f"{script['path']} 退出码 0", "ok")

        # 5. 断线型机器：等回来、等后台单元结束
        if host.get("detach"):
            if not self._wait_connection(host, emit):
                return fail(f"等待 NetBird 重连超时（{self.s.detach_wait_timeout}s）")
            pattern = host.get("detach_unit_pattern") or "netbird-upgrade-*"
            if not self._wait_unit(host, pattern, emit):
                return fail(f"后台升级单元 {pattern} 未在 {self.s.detach_poll_timeout}s 内结束")
            log_path = host.get("detach_log") or "/var/log/netbird-upgrade.log"
            tail = self.transport.run(host, ["tail", "-n", "20", log_path], timeout=30)
            for line in tail.lines:
                if line.strip():
                    emit(name, "  " + line, "dim")

        # 6. 更新后版本
        result["version_after"] = self._version(host)
        emit(
            name,
            f"版本 {result['version_before'] or '未安装'} → {result['version_after'] or '未安装'}",
            "ok",
        )
        result["duration"] = round(time.monotonic() - started, 1)
        return result

    # ---------- 小工具 ----------

    def _version(self, host) -> str | None:
        run = self.transport.run(host, ["netbird", "version"], timeout=30)
        if not run.ok:
            return None
        match = _VERSION_RE.search(run.text)
        if match:
            return match.group(1)
        return run.first_line or None

    def _wait_connection(self, host, emit) -> bool:
        emit(host["name"], "等待 NetBird 断线重连", "dim")
        deadline = time.monotonic() + self.s.detach_wait_timeout
        self.sleep(self.s.detach_connect_interval)
        while time.monotonic() < deadline:
            run = self.transport.run(host, ["true"], timeout=self.s.connect_timeout + 5)
            if run.ok:
                emit(host["name"], "SSH 已恢复", "dim")
                return True
            self.sleep(self.s.detach_connect_interval)
        return False

    def _wait_unit(self, host, pattern: str, emit) -> bool:
        emit(host["name"], f"等待后台单元 {pattern}", "dim")
        deadline = time.monotonic() + self.s.detach_poll_timeout
        while time.monotonic() < deadline:
            run = self.transport.run(
                host,
                ["systemctl", "list-units", "--all", "--plain", "--no-legend", pattern],
                timeout=30,
            )
            if not any(pattern.replace("*", "") in line for line in run.lines):
                emit(host["name"], "后台单元已结束", "dim")
                return True
            self.sleep(self.s.detach_poll_interval)
        return False
