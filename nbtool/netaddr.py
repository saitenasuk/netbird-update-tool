"""地址分类：判断一个地址是局域网、公网还是走 netbird 隧道。

这个判断直接对应一个真实风险：用 netbird 地址（100.64.0.0/10）连的机器，
升级它自己的客户端时 SSH 必断。界面据此给出警告。
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess

NETBIRD_NET = ipaddress.ip_network("100.64.0.0/10")

_LABELS = {
    "netbird": "netbird 隧道",
    "lan": "局域网",
    "public": "公网",
    "loopback": "本机回环",
    "hostname": "主机名",
    "unknown": "未知",
}


def classify(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "hostname"
    if ip.is_loopback:
        return "loopback"
    if ip in NETBIRD_NET:
        return "netbird"
    if ip.is_private:
        return "lan"
    return "public"


def label(kind: str) -> str:
    return _LABELS.get(kind, kind)


def local_addresses() -> set[str]:
    """尽力收集本机地址，只用于「这台是不是工具所在主机」的提示，失败就算了。"""
    found: set[str] = set()
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        ).stdout
        found.update(out.split())
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except Exception:
        pass
    return found


def is_local(address: str, locals_: set[str] | None = None) -> bool:
    if address in {"127.0.0.1", "localhost", "::1"}:
        return True
    pool = local_addresses() if locals_ is None else locals_
    return address in pool


def describe(address: str, locals_: set[str] | None = None) -> dict:
    kind = classify(address)
    return {
        "kind": kind,
        "label": label(kind),
        "is_local": is_local(address, locals_),
        "warn": (
            "这个地址走 netbird 隧道，升级客户端会断开 SSH；请改填局域网/公网 IP，或勾选「后台执行」"
            if kind == "netbird"
            else ""
        ),
    }
