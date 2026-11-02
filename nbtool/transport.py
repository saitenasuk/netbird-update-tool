"""SSH 执行层：把系统 ssh 客户端当成子进程用。

为什么不用 paramiko：OpenSSH 能处理所有私钥格式、known_hosts 语义和 agent，
Python 里不需要碰私钥；而且容器里本来就要装 openssh-client。

每条命令一个独立 ssh 进程，不使用 ControlMaster —— Windows 上也能跑。
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Result:
    rc: int
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.cancelled

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def first_line(self) -> str:
        for line in self.lines:
            if line.strip():
                return line.strip()
        return ""


def host_target(host: dict) -> str:
    return f"{host.get('ssh_user') or 'root'}@{host['address']}"


class SshTransport:
    """真正跑 ssh 的传输层。"""

    def __init__(
        self,
        keys_dir: Path,
        known_hosts: Path,
        connect_timeout: int = 10,
        ssh_bin: str | None = None,
    ):
        self.keys_dir = Path(keys_dir)
        self.known_hosts = Path(known_hosts)
        self.connect_timeout = connect_timeout
        self.ssh_bin = ssh_bin or os.environ.get("NB_SSH_BIN") or "ssh"
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.touch(exist_ok=True)

    def argv(self, host: dict, remote_argv: list[str]) -> list[str]:
        args = [
            self.ssh_bin,
            "-F",
            "/dev/null",  # 不读宿主机的用户配置，容器里行为可预期
            "-o",
            "BatchMode=yes",  # 绝不交互，避免任务卡在密码提示上
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "StrictHostKeyChecking=accept-new",  # 首次连接 TOFU
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(host.get("ssh_port") or 22),
        ]
        key_name = host.get("key_name")
        if key_name:
            args += ["-o", "IdentitiesOnly=yes", "-i", str(self.keys_dir / key_name)]
        args.append(host_target(host))
        args.append(shlex.join(remote_argv))
        return args

    def run(
        self,
        host: dict,
        argv: list[str],
        timeout: float | None = None,
        on_line=None,
        cancel=None,
    ) -> Result:
        cmd = self.argv(host, argv)
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            return Result(rc=127, lines=[f"找不到 ssh 客户端：{self.ssh_bin}"])

        lines: list[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        # 0.5 秒一轮地看是否超时/被中止，这样「中止」能真正杀掉正在跑的脚本
        started = time.monotonic()
        timed_out = cancelled = False
        while True:
            try:
                rc = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel():
                    cancelled = True
                    _kill(proc)
                    rc = proc.wait()
                    break
                if timeout is not None and time.monotonic() - started > timeout:
                    timed_out = True
                    _kill(proc)
                    rc = proc.wait()
                    break
        thread.join(timeout=5)
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        return Result(rc=rc, lines=lines, timed_out=timed_out, cancelled=cancelled)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class FakeTransport:
    """测试用：按脚本化的应答返回，不碰网络。

    responses: {(address, tuple(argv)): Result} 或 {tuple(argv): Result}；
    没命中的命令返回 rc=0 和空输出。
    """

    def __init__(self, responses: dict | None = None, default: Result | None = None):
        self.responses = responses or {}
        self.default = default or Result(rc=0, lines=[])
        self.calls: list[tuple] = []

    def run(self, host, argv, timeout=None, on_line=None, cancel=None) -> Result:
        self.calls.append((host.get("address"), tuple(argv)))
        key = (host.get("address"), tuple(argv))
        result = self.responses.get(key)
        if result is None:
            result = self.responses.get(tuple(argv), self.default)
        if callable(result):
            result = result(host, argv)
        for line in result.lines:
            if on_line:
                on_line(line)
        return result

    def called(self, *argv) -> bool:
        return any(call[1] == argv for call in self.calls)
