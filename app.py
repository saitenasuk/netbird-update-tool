#!/usr/bin/env python3
"""NetBird 更新台 — 入口。

env:
  NB_BIND          监听地址，默认 127.0.0.1
  NB_PORT          端口，默认 8770
  NB_DATA          数据目录，默认 ./data（容器里是 /data）
  NB_COM_PASSWORD  Basic 认证密码；不设则不校验（仅本机调试用）
  NB_AUTH_USER     认证用户名，默认 admin
  NB_SEED          首次启动导入的种子 JSON（库为空时生效）
  NB_SSH_BIN       ssh 客户端路径，默认 ssh
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from nbtool.db import Store
from nbtool.engine import Engine, Settings
from nbtool.jobs import JobManager
from nbtool.keys import KeyStore
from nbtool.transport import SshTransport
from nbtool.web import App, make_server


def seed_if_empty(store: Store, keys: KeyStore, path: Path) -> None:
    if store.list_hosts() or not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[seed] 跳过：{path} 解析失败（{exc}）", file=sys.stderr)
        return
    for host in data.get("hosts") or []:
        payload = dict(host)
        scripts = payload.get("scripts") or []
        if isinstance(scripts, list) and scripts and isinstance(scripts[0], dict):
            lines = []
            for script in scripts:
                args = " ".join(script.get("args") or [])
                lines.append(f"{script['path']} | {script.get('label') or ''} | {args}")
            payload["scripts"] = "\n".join(lines)
        try:
            store.create_host(payload)
            print(f"[seed] 导入 {payload.get('name')}")
        except Exception as exc:
            print(f"[seed] {payload.get('name')} 导入失败：{exc}", file=sys.stderr)
    for key in data.get("keys") or []:
        try:
            if not keys.exists(key["name"]):
                keys.add(key["name"], key["private_key"])
                print(f"[seed] 导入密钥 {key['name']}")
        except Exception as exc:
            print(f"[seed] 密钥 {key.get('name')} 导入失败：{exc}", file=sys.stderr)


def main() -> int:
    data_dir = Path(os.environ.get("NB_DATA", "data")).expanduser().resolve()
    try:
        for sub in ("keys", "jobs"):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        print(f"无法写入数据目录 {data_dir}（当前 uid={uid}）", file=sys.stderr)
        print(
            "容器镜像会自动处理属主；如果你是直接跑 python app.py，先执行：\n"
            f"  sudo chown -R $(id -u):$(id -g) {data_dir}\n"
            "或换一个可写目录：NB_DATA=/some/writable/dir python3 app.py",
            file=sys.stderr,
        )
        return 1

    store = Store(str(data_dir / "db.sqlite3"))
    keys = KeyStore(data_dir / "keys")
    settings = Settings()
    transport = SshTransport(keys.dir, data_dir / "known_hosts")
    engine = Engine(transport, settings)
    jobs = JobManager(store, data_dir / "jobs", settings.keep_jobs)

    seed_path = Path(os.environ.get("NB_SEED", data_dir / "seed.json")).expanduser()
    seed_if_empty(store, keys, seed_path)

    password = os.environ.get("NB_COM_PASSWORD") or None
    app = App(
        store,
        keys,
        engine,
        jobs,
        transport,
        password=password,
        auth_user=os.environ.get("NB_AUTH_USER", "admin"),
    )

    bind = os.environ.get("NB_BIND", "127.0.0.1")
    port = int(os.environ.get("NB_PORT", "8770"))
    httpd = make_server(app, bind, port)
    print(f"NetBird 更新台 · http://{bind}:{port} · 数据目录 {data_dir}")
    if not password:
        print("警告：未设置 NB_COM_PASSWORD，任何人可访问并触发更新脚本", file=sys.stderr)
    elif password in {"换成你的密码", "CHANGE-ME"}:
        print("警告：NB_COM_PASSWORD 还是示例里的占位值，请改成自己的密码", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
