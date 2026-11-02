"""HTTP 层：路由、Basic 认证、SSE、静态文件。只用标准库。

安全边界：界面只能触发数据库里登记过的脚本路径 + 该脚本声明的选项名，
没有任何执行任意命令的入口。
"""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import netaddr
from .db import Store, ValidationError, parse_scripts
from .engine import Engine
from .jobs import JobManager
from .keys import KeyError_, KeyStore, bootstrap_command

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class App:
    def __init__(
        self,
        store: Store,
        keys: KeyStore,
        engine: Engine,
        jobs: JobManager,
        transport,
        password: str | None = None,
        auth_user: str = "admin",
    ):
        self.store = store
        self.keys = keys
        self.engine = engine
        self.jobs = jobs
        self.transport = transport
        self.password = password
        self.auth_user = auth_user
        self.checks: dict[int, dict] = {}  # host_id -> 最近一次体检结果
        self.locals = netaddr.local_addresses()
        self._lock = threading.Lock()

    # ---------- 数据成形 ----------

    def host_view(self, host: dict) -> dict:
        view = dict(host)
        view.pop("created_at", None)
        view["addr"] = netaddr.describe(host["address"], self.locals)
        view["check"] = self.checks.get(host["id"], {})
        view["bootstrap"] = ""
        if host.get("key_name") and self.keys.exists(host["key_name"]):
            try:
                pub = self.keys.public_key(host["key_name"])
                view["bootstrap"] = bootstrap_command(pub, host.get("ssh_user") or "root")
            except KeyError_:
                pass
        return view

    def state(self) -> dict:
        # 展示顺序跟执行顺序一致：console 组在前，self_host 在组内最后
        hosts = [self.host_view(h) for h in self.engine.order(self.store.list_hosts())]
        active = self.jobs.active()
        return {
            "hosts": hosts,
            "keys": self.keys.list(),
            "jobs": self.store.list_jobs(20),
            "active": active.to_dict() if active else None,
            "group_order": self.engine.s.group_order,
            "server": {"python": os.sys.version.split()[0], "auth": bool(self.password)},
        }

    # ---------- 机器 ----------

    def save_host(self, payload: dict, host_id: int | None = None) -> dict:
        payload = dict(payload)
        payload["scripts_parsed"] = parse_scripts(payload.get("scripts") or "")
        if payload.get("key_name") and not self.keys.exists(payload["key_name"]):
            raise ValidationError(f"密钥 {payload['key_name']} 不存在")
        if host_id is None:
            host_id = self.store.create_host(payload)
        else:
            if self.store.get_host(host_id) is None:
                raise ValidationError("机器不存在")
            self.store.update_host(host_id, payload)
            with self._lock:
                self.checks.pop(host_id, None)
        return self.host_view(self.store.get_host(host_id))

    def check_host(self, host: dict) -> dict:
        """体检：连通性、脚本可执行、当前版本。"""
        result = {"at": _now(), "reachable": False, "error": "", "version": None, "scripts": []}
        ping = self.transport.run(host, ["true"], timeout=self.engine.s.connect_timeout + 5)
        if not ping.ok:
            lines = [l for l in ping.lines if l.strip()]
            result["error"] = lines[-1] if lines else "SSH 连不上"
            with self._lock:
                self.checks[host["id"]] = result
            return result
        result["reachable"] = True
        version = self.transport.run(host, ["netbird", "version"], timeout=30)
        if version.ok:
            match = __import__("re").search(r"(\d+\.\d+\.\d+[-\w.]*)", version.text)
            result["version"] = match.group(1) if match else version.first_line
        for script in host.get("scripts", []):
            probe = self.transport.run(host, ["test", "-x", script["path"]], timeout=30)
            result["scripts"].append({"path": script["path"], "ok": probe.ok})
        with self._lock:
            self.checks[host["id"]] = result
        return result

    # ---------- 任务 ----------

    def build_targets(self, requested: list[dict]) -> list[dict]:
        hosts = {h["id"]: h for h in self.store.list_hosts()}
        targets = []
        for item in requested:
            host_id = int(item.get("host_id") or 0)
            host = hosts.get(host_id)
            if host is None:
                raise ValidationError(f"机器 {host_id} 不存在")
            args_map = item.get("args") or {}
            scripts = []
            for script in host["scripts"]:
                raw = args_map.get(str(script["id"]), args_map.get(script["id"]))
                if raw is None:
                    # 没传就按「全选」处理：带选项的脚本默认跑全部组件
                    chosen = list(script["options"])
                else:
                    chosen = [str(a) for a in raw]
                unknown = [a for a in chosen if a not in script["options"]]
                if unknown:
                    raise ValidationError(
                        f"{host['name']} 的脚本 {script['path']} 不接受参数 {','.join(unknown)}"
                    )
                scripts.append({"path": script["path"], "args": chosen, "timeout": script.get("timeout")})
            targets.append({"host": host, "scripts": scripts})
        if not targets:
            raise ValidationError("没有选择任何机器")
        ordered = self.engine.order([t["host"] for t in targets])
        by_id = {t["host"]["id"]: t for t in targets}
        return [by_id[h["id"]] for h in ordered]

    def start_job(self, requested: list[dict], label: str = "") -> dict:
        targets = self.build_targets(requested)
        spec = {
            "label": label,
            "targets": [
                {
                    "host_id": t["host"]["id"],
                    "name": t["host"]["name"],
                    "group": t["host"].get("group_name"),
                    "scripts": [{"path": s["path"], "args": s["args"]} for s in t["scripts"]],
                }
                for t in targets
            ],
        }
        job = self.jobs.create(spec)
        engine = self.engine

        def worker():
            return engine.run(targets, job.emit, job.is_aborted)

        self.jobs.start(job, worker)
        return job.to_dict()


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "nbtool/0.1"
    app: App  # 由 make_server 注入

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # 静音默认访问日志，出错时另有输出
        pass

    def _authorized(self) -> bool:
        app = self.app
        if not app.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = raw.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, app.auth_user) and hmac.compare_digest(password, app.password)

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc

    # ---------- 路由 ----------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_HEAD(self):
        self._route("GET")

    def _route(self, method: str):
        path = urlparse(self.path).path
        if path == "/healthz":  # 健康检查不需要认证
            return self._json({"ok": True})
        if not self._authorized():
            self._send(
                401,
                '{"error":"需要认证"}'.encode("utf-8"),
                extra={"WWW-Authenticate": 'Basic realm="nbtool"'},
            )
            return
        try:
            if path == "/api/state" and method == "GET":
                return self._json(self.app.state())

            if path == "/api/hosts" and method == "GET":
                return self._json([self.app.host_view(h) for h in self.app.store.list_hosts()])
            if path == "/api/hosts" and method == "POST":
                return self._json(self.app.save_host(self._body()), 201)
            if path == "/api/hosts/test" and method == "POST":
                return self._json(self._test_connection())
            if path.startswith("/api/hosts/"):
                return self._host_route(path, method)

            if path == "/api/keys" and method == "GET":
                return self._json(self.app.keys.list())
            if path == "/api/keys" and method == "POST":
                return self._json(self._create_key(), 201)
            if path.startswith("/api/keys/") and method == "DELETE":
                return self._json(self._delete_key(unquote(path.rsplit("/", 1)[-1])))

            if path == "/api/jobs" and method == "POST":
                body = self._body()
                return self._json(self.app.start_job(body.get("targets") or [], body.get("label") or ""), 202)
            if path.startswith("/api/jobs/"):
                return self._job_route(path, method)

            return self._static(path)
        except ValidationError as exc:
            return self._error(400, str(exc))
        except KeyError_ as exc:
            return self._error(400, str(exc))
        except RuntimeError as exc:
            return self._error(409, str(exc))
        except BrokenPipeError:
            return
        except Exception:
            traceback.print_exc()
            return self._error(500, "服务端错误，详见容器日志")

    # ---------- 子路由 ----------

    def _host_route(self, path: str, method: str):
        parts = [p for p in path.split("/") if p]  # api hosts <id> [action]
        if len(parts) < 3:
            return self._error(404, "未知路由")
        try:
            host_id = int(parts[2])
        except ValueError:
            return self._error(404, "未知路由")
        host = self.app.store.get_host(host_id)
        if host is None:
            return self._error(404, "机器不存在")

        action = parts[3] if len(parts) > 3 else ""
        if action == "bootstrap" and method == "GET":
            view = self.app.host_view(host)
            return self._json({"command": view["bootstrap"], "addr": view["addr"]})
        if action == "check" and method == "POST":
            result = self.app.check_host(host)
            return self._json({"host": self.app.host_view(self.app.store.get_host(host_id)), "check": result})
        if action:
            return self._error(404, "未知路由")
        if method == "GET":
            return self._json(self.app.host_view(host))
        if method == "PUT":
            return self._json(self.app.save_host(self._body(), host_id))
        if method == "DELETE":
            if self.app.jobs.active():
                return self._error(409, "任务运行中，不能删除机器")
            self.app.store.delete_host(host_id)
            return self._json({"ok": True})
        return self._error(405, "方法不允许")

    def _job_route(self, path: str, method: str):
        parts = [p for p in path.split("/") if p]
        job_id = parts[2] if len(parts) > 2 else ""
        action = parts[3] if len(parts) > 3 else ""

        if action == "events" and method == "GET":
            return self._sse(job_id)
        if action == "log" and method == "GET":
            text = self.app.jobs.read_log(job_id)
            return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
        if action == "abort" and method == "POST":
            job = self.app.jobs.get(job_id)
            if job is None:
                return self._error(404, "任务不存在")
            job.abort()
            return self._json({"ok": True})
        if action:
            return self._error(404, "未知路由")

        job = self.app.jobs.get(job_id)
        if job is not None:
            return self._json(job.to_dict())
        stored = self.app.store.get_job(job_id)
        if stored is None:
            return self._error(404, "任务不存在")
        return self._json(stored)

    def _test_connection(self) -> dict:
        payload = self._body()
        key_name = (payload.get("key_name") or "").strip()
        if key_name and not self.app.keys.exists(key_name):
            raise ValidationError(f"密钥 {key_name} 不存在")
        host = {
            "id": 0,
            "name": payload.get("name") or payload.get("address") or "",
            "address": (payload.get("address") or "").strip(),
            "ssh_user": (payload.get("ssh_user") or "root").strip(),
            "ssh_port": int(payload.get("ssh_port") or 22),
            "key_name": key_name,
        }
        if not host["address"]:
            raise ValidationError("地址不能为空")
        result = self.app.check_host(host)
        return {
            "ok": result["reachable"],
            "error": result["error"],
            "version": result["version"],
            "addr": netaddr.describe(host["address"], self.app.locals),
        }

    def _create_key(self) -> dict:
        payload = self._body()
        name = (payload.get("name") or "").strip()
        if payload.get("generate"):
            return self.app.keys.generate(name)
        content = payload.get("private_key") or ""
        return self.app.keys.add(name, content)

    def _delete_key(self, name: str) -> dict:
        used = [h["name"] for h in self.app.store.list_hosts() if h.get("key_name") == name]
        if used:
            raise ValidationError(f"密钥仍被这些机器使用：{', '.join(used)}")
        self.app.keys.delete(name)
        return {"ok": True}

    def _sse(self, job_id: str):
        job = self.app.jobs.get(job_id)
        if job is None:
            return self._error(404, "任务不存在")
        query = urlparse(self.path).query
        since = 0
        for part in query.split("&"):
            if part.startswith("from="):
                try:
                    since = int(part[5:])
                except ValueError:
                    since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        cursor = since
        try:
            while True:
                lines, done = job.poll(cursor, timeout=15.0)
                if lines:
                    for entry in lines:
                        cursor = entry["seq"]
                        payload = json.dumps(entry, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": ping\n\n")
                payload = json.dumps({"status": job.status, "last_seq": cursor}, ensure_ascii=False)
                self.wfile.write(f"event: status\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if done and not lines:
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---------- 静态文件 ----------

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/") :]
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self._error(403, "forbidden")
        if not target.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self._send(200, body, ctype)


def make_server(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return httpd
