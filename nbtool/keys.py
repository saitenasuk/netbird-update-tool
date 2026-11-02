"""SSH 密钥：生成、导入、导出公钥、拼装目标机上的安装命令。

私钥以 600 权限存在 <data>/keys/ 下，明文、无主密码——这是零依赖的取舍，
能读 volume 就能读私钥。运行方式决定了这是可接受的：只绑内网 + Basic 认证。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

BOOTSTRAP_TEMPLATE = """mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo '{pubkey}' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys"""


class KeyError_(ValueError):
    """密钥相关错误，直接展示给用户。"""


class KeyStore:
    def __init__(self, directory: Path, ssh_keygen: str = "ssh-keygen"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.ssh_keygen = ssh_keygen

    # ---------- 查询 ----------

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.iterdir()):
            if not p.is_file() or p.name.endswith(".pub"):
                continue
            try:
                pub = self.public_key(p.name)
            except KeyError_:
                pub = ""
            out.append(
                {
                    "name": p.name,
                    "public_key": pub,
                    "created_at": _mtime_iso(p),
                }
            )
        return out

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def path(self, name: str) -> Path:
        if not _NAME_RE.match(name or ""):
            raise KeyError_("密钥名只能用字母数字和 . _ -")
        return self.dir / name

    def public_key(self, name: str) -> str:
        p = self.path(name)
        if not p.is_file():
            raise KeyError_(f"密钥 {name} 不存在")
        # ssh-keygen -y 会尝试读 stdin 要密码短语，所以必须关掉 stdin 并设超时
        try:
            proc = subprocess.run(
                [self.ssh_keygen, "-y", "-f", str(p)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except subprocess.TimeoutExpired as exc:
            raise KeyError_("私钥读取超时，可能带密码短语") from exc
        if proc.returncode != 0:
            raise KeyError_(
                "无法导出公钥：私钥可能带密码短语或格式不受支持（只接受无密码短语的密钥）"
            )
        return proc.stdout.strip()

    # ---------- 写入 ----------

    def add(self, name: str, private_key: str) -> dict:
        name = (name or "").strip()
        p = self.path(name)
        if p.exists():
            raise KeyError_(f"密钥 {name} 已存在")
        text = private_key.strip() + "\n"
        if "PRIVATE KEY" not in text:
            raise KeyError_("这看起来不是私钥内容（缺少 PRIVATE KEY 标记）")
        p.write_text(text, encoding="utf-8")
        os.chmod(p, 0o600)
        try:
            pub = self.public_key(name)
        except KeyError_:
            p.unlink(missing_ok=True)
            raise
        return {"name": name, "public_key": pub, "created_at": _mtime_iso(p)}

    def generate(self, name: str, comment: str = "nbtool") -> dict:
        p = self.path((name or "").strip())
        if p.exists():
            raise KeyError_(f"密钥 {name} 已存在")
        proc = subprocess.run(
            [self.ssh_keygen, "-t", "ed25519", "-N", "", "-C", comment, "-f", str(p)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise KeyError_(f"生成失败：{proc.stderr.strip() or proc.stdout.strip()}")
        os.chmod(p, 0o600)
        return {"name": p.name, "public_key": self.public_key(p.name), "created_at": _mtime_iso(p)}

    def delete(self, name: str) -> None:
        p = self.path(name)
        p.unlink(missing_ok=True)
        Path(str(p) + ".pub").unlink(missing_ok=True)


def bootstrap_command(public_key: str, user: str = "root") -> str:
    """目标机上执行的安装命令。

    以 root 为例直接用 ~；换成别的用户时提示以该用户身份执行。
    """
    body = BOOTSTRAP_TEMPLATE.format(pubkey=public_key.strip())
    if user and user != "root":
        return f"# 以 {user} 身份执行\n" + body
    return body


def _mtime_iso(p: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
