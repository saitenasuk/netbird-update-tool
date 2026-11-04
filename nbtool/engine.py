"""任务编排：顺序、门禁、前置检查、detach 等待、版本对比。

失败语义：默认「跑完所有机器」，失败的那台标红，最后汇总；需要时可用
stop_on_failure=True 改成遇错即停。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

_VERSION_LINE = re.compile(r"^v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$")
_VERSION_LABELED = re.compile(
    r"(?:netbird|daemon)(?:\s+version)?[:\s]+v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$", re.I
)


def parse_netbird_version(text: str) -> str | None:
    """从 `netbird version` 的输出里取版本号。

    输出里可能混着登录横幅（Proxmox 的 LXC 会打印内核版本，例如 7.0.12-1-pve），
    所以从后往前找「整行就是一个版本号」的行，而不是取全文第一个数字串。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        match = _VERSION_LINE.match(line)
        if match:
            return match.group(1)
    for line in reversed(lines):
        match = _VERSION_LABELED.search(line)
        if match:
            return match.group(1)
    return None


@dataclass
class Settings:
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
        """优先级数字小的先跑；本机永远排最后；同优先级按 sort_order、id。"""

        def rank(host: dict):
            return (
                1 if host.get("self_host") else 0,  # 更新自己会断自己的网，永远压到最后
                int(host.get("priority") if host.get("priority") is not None else 10),
                int(host.get("sort_order") or 0),
                int(host["id"]),
            )

        return sorted(hosts, key=rank)

    # ---------- 执行 ----------

    def run(
        self,
        targets: list[dict],
        emit,
        is_aborted=lambda: False,
        stop_on_failure: bool = False,
    ) -> dict:
        results: list[dict] = []
        aborted = False
        stopped = False
        total = len(targets)
        names = " → ".join(t["host"]["name"] for t in targets[:6])
        more = " …" if total > 6 else ""
        emit("", f"任务开始 · {total} 台目标 · 顺序 {names}{more}", "dim")

        for index, target in enumerate(targets, 1):
            host = target["host"]
            if is_aborted():
                aborted = True
                emit(host["name"], "已中止，剩余机器未执行", "err")
                break
            emit("", f"──── {index}/{total} {host['name']} ────", "dim")
            result = self._run_host(host, target["scripts"], emit, is_aborted)
            results.append(result)
            if result["status"] != "ok" and stop_on_failure:
                emit(
                    host["name"],
                    f"{host['name']} 失败，已按设置中止后续机器（还剩 {total - index} 台）",
                    "err",
                )
                stopped = True
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
        if stopped:
            emit("", f"任务中止 · {len(results)}/{total} 台已处理", "err")
        else:
            failed = [r["name"] for r in results if r["status"] != "ok"]
            summary = f"任务结束 · {len(results)}/{total} 台已处理"
            if failed:
                summary += f" · 失败 {len(failed)} 台：{', '.join(failed)}"
            emit("", summary, "ok" if not failed else "err")
        return {"status": status, "hosts": results}

    def _run_host(self, host, scripts, emit, is_aborted) -> dict:
        name = host["name"]
        started = time.monotonic()
        result = {
            "host_id": host.get("id"),
            "name": name,
            "priority": host.get("priority"),
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
        return parse_netbird_version(run.text)

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
