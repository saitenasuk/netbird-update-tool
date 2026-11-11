"""HTTP 层测试：真实起服务，用 urllib 打接口（FakeTransport，不碰网络）。"""

import base64
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbtool.db import Store  # noqa: E402
from nbtool.engine import Engine, Settings  # noqa: E402
from nbtool.jobs import JobManager  # noqa: E402
from nbtool.keys import KeyStore  # noqa: E402
from nbtool.transport import FakeTransport, Result  # noqa: E402
from nbtool.web import App, make_server  # noqa: E402

PASSWORD = "s3cret"


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "keys").mkdir()
        (data / "jobs").mkdir()
        cls.store = Store(str(data / "db.sqlite3"))
        cls.keys = KeyStore(data / "keys")
        fake = FakeTransport(
            {
                ("true",): Result(0),
                ("netbird", "version"): Result(0, ["0.67.0"]),
                ("test", "-x", "/root/up.sh"): Result(0),
                ("/root/up.sh",): Result(0, ["升级中", "完成"]),
                ("/root/up.sh", "a", "b"): Result(0, ["升级中", "完成"]),
            }
        )
        cls.fake = fake
        engine = Engine(fake, Settings(), sleep=lambda _s: None)
        cls.jobs = JobManager(cls.store, data / "jobs")
        app = App(cls.store, cls.keys, engine, cls.jobs, fake, password=PASSWORD)
        cls.server = make_server(app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # ---------- helpers ----------

    def request(self, method, path, body=None, auth=True):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def json_request(self, method, path, body=None, auth=True):
        status, text = self.request(method, path, body, auth)
        return status, (json.loads(text) if text else None)

    # ---------- 认证 ----------

    def test_requires_auth(self):
        status, _ = self.request("GET", "/api/state", auth=False)
        self.assertEqual(status, 401)

    def test_wrong_password_rejected(self):
        url = f"http://127.0.0.1:{self.port}/api/state"
        req = urllib.request.Request(url)
        token = base64.b64encode(b"admin:wrong").decode()
        req.add_header("Authorization", f"Basic {token}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_state_ok(self):
        status, data = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("hosts", data)
        self.assertEqual(data["defaults"]["script_timeout"], 1800)

    # ---------- 机器 CRUD ----------

    def test_create_validate_and_delete_host(self):
        bad = {"name": "有 空格", "address": "10.0.0.1", "scripts": "/root/a.sh"}
        status, data = self.json_request("POST", "/api/hosts", bad)
        self.assertEqual(status, 400)
        self.assertIn("名称", data["error"])

        good = {
            "name": "unit-host",
            "address": "10.0.0.5",
            "ssh_user": "root",
            "ssh_port": 22,
            "group_name": "clients",
            "scripts": "/root/up.sh | 更新 | a b",
        }
        status, host = self.json_request("POST", "/api/hosts", good)
        self.assertEqual(status, 201)
        self.assertEqual(host["name"], "unit-host")
        self.assertEqual(host["scripts"][0]["args"], ["a", "b"])
        self.assertIn("addr", host)

        status, hosts = self.json_request("GET", "/api/hosts")
        self.assertTrue(any(h["name"] == "unit-host" for h in hosts))

        status, _ = self.json_request("PUT", f"/api/hosts/{host['id']}", dict(good, note="改过"))
        self.assertEqual(status, 200)

        status, _ = self.json_request("DELETE", f"/api/hosts/{host['id']}")
        self.assertEqual(status, 200)

    def test_check_host(self):
        payload = {
            "name": "check-host",
            "address": "100.64.0.9",
            "scripts": "/root/up.sh | 更新 |",
        }
        _, host = self.json_request("POST", "/api/hosts", payload)
        status, data = self.json_request("POST", f"/api/hosts/{host['id']}/check")
        self.assertEqual(status, 200)
        self.assertTrue(data["check"]["reachable"])
        self.assertEqual(data["check"]["version"], "0.67.0")
        self.assertEqual(data["host"]["addr"]["kind"], "netbird")
        self.assertTrue(data["host"]["addr"]["warn"])

    def test_connection_test_rejects_unknown_key(self):
        status, data = self.json_request(
            "POST", "/api/hosts/test", {"address": "10.0.0.1", "key_name": "nope"}
        )
        self.assertEqual(status, 400)
        self.assertIn("密钥", data["error"])

    # ---------- 任务 ----------

    def test_job_uses_script_args_from_config(self):
        payload = {"name": "arg-host", "address": "10.0.0.7", "scripts": "/root/up.sh | 更新 | zzz"}
        _, host = self.json_request("POST", "/api/hosts", payload)
        status, job = self.json_request("POST", "/api/jobs", {"targets": [{"host_id": host["id"]}]})
        self.assertEqual(status, 202)
        self.assertEqual(job["spec"]["targets"][0]["scripts"][0]["args"], ["zzz"])

    def test_job_runs_and_streams(self):
        payload = {"name": "run-host", "address": "10.0.0.8", "scripts": "/root/up.sh | 更新 | a b"}
        _, host = self.json_request("POST", "/api/hosts", payload)
        status, job = self.json_request("POST", "/api/jobs", {"targets": [{"host_id": host["id"]}]})
        self.assertEqual(status, 202)

        # SSE：先补发全部历史，再随任务结束关闭
        url = f"http://127.0.0.1:{self.port}/api/jobs/{job['id']}/events?from=0"
        req = urllib.request.Request(url)
        token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        seen = []
        with urllib.request.urlopen(req, timeout=15) as res:
            deadline = time.time() + 12
            buffer = b""
            while time.time() < deadline:
                chunk = res.read(1)
                if not chunk:
                    break
                buffer += chunk
                while b"\n\n" in buffer:
                    frame, buffer = buffer.split(b"\n\n", 1)
                    text = frame.decode()
                    if "data: " in text and "event: status" not in text:
                        entry = json.loads(text.split("data: ", 1)[1].strip())
                        seen.append(entry["text"])
                    if '"status": "' in text and '"running"' not in text:
                        deadline = 0
                        break
        joined = "\n".join(seen)
        self.assertIn("任务开始", joined)
        self.assertIn("升级中", joined, "脚本输出必须被流式转发")
        self.assertIn("退出码 0", joined)

        time.sleep(0.3)
        status, stored = self.json_request("GET", f"/api/jobs/{job['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(stored["status"], "success")
        self.assertEqual(stored["result"]["hosts"][0]["version_after"], "0.67.0")

    def test_concurrent_job_conflict(self):
        # 手工造一个「运行中」的任务，模拟锁被占用
        self.jobs.create({"label": "占位", "targets": []})
        try:
            payload = {"name": "busy-host", "address": "10.0.0.9", "scripts": "/root/up.sh | 更新 |"}
            _, host = self.json_request("POST", "/api/hosts", payload)
            status, data = self.json_request("POST", "/api/jobs", {"targets": [{"host_id": host["id"]}]})
            self.assertEqual(status, 409)
            self.assertIn("运行", data["error"])
        finally:
            self.jobs.active().finish("aborted", None)

    # ---------- 登录页 ----------

    def _raw(self, method, path, body=None, cookie=None):
        """不跟随跳转，方便断言 302 和 Set-Cookie。"""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with opener.open(req, timeout=10) as res:
                return res.status, dict(res.headers), res.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def test_login_page_is_public(self):
        status, _, html = self._raw("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("登录", html)
        # 登录页依赖的静态资源也要放行
        self.assertEqual(self._raw("GET", "/style.css")[0], 200)
        self.assertEqual(self._raw("GET", "/login.js")[0], 200)

    def test_index_redirects_to_login(self):
        status, headers, _ = self._raw("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")

    def test_api_returns_login_required(self):
        status, _, body = self._raw("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertTrue(json.loads(body)["login_required"])

    def test_login_sets_cookie_and_works(self):
        status, headers, _ = self._raw(
            "POST", "/api/login", {"user": "admin", "password": PASSWORD}
        )
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("nbtool_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        token = cookie.split(";")[0]
        status, _, _ = self._raw("GET", "/api/state", cookie=token)
        self.assertEqual(status, 200)

    def test_wrong_password_rejected(self):
        status, _, body = self._raw("POST", "/api/login", {"user": "admin", "password": "nope"})
        self.assertEqual(status, 401)
        self.assertIn("不对", json.loads(body)["error"])

    def test_logout_invalidates_session(self):
        _, headers, _ = self._raw("POST", "/api/login", {"user": "admin", "password": PASSWORD})
        token = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self._raw("GET", "/api/state", cookie=token)[0], 200)
        self.assertEqual(self._raw("POST", "/api/logout", cookie=token)[0], 200)
        self.assertEqual(self._raw("GET", "/api/state", cookie=token)[0], 401, "登出后旧 cookie 应失效")

    def test_static_index_served(self):
        status, text = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("NetBird 更新台", text)

    def test_path_traversal_blocked(self):
        status, _ = self.request("GET", "/../app.py")
        self.assertIn(status, (403, 404))


class UnicodePasswordTest(unittest.TestCase):
    """密码是中文也要能登录——README 的示例密码就是中文。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "keys").mkdir()
        (data / "jobs").mkdir()
        store = Store(str(data / "db.sqlite3"))
        keys = KeyStore(data / "keys")
        fake = FakeTransport()
        engine = Engine(fake, Settings(), sleep=lambda _s: None)
        jobs = JobManager(store, data / "jobs")
        cls.password = "换成你的密码"
        app = App(store, keys, engine, jobs, fake, password=cls.password, auth_user="admin")
        cls.server = make_server(app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _status(self, password):
        url = f"http://127.0.0.1:{self.port}/api/state"
        req = urllib.request.Request(url)
        token = base64.b64encode(f"admin:{password}".encode("utf-8")).decode()
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_chinese_password_works(self):
        self.assertEqual(self._status(self.password), 200)

    def test_chinese_password_still_rejects_wrong_value(self):
        self.assertEqual(self._status("换成你的密码2"), 401)

    def test_garbage_auth_header_is_401_not_disconnect(self):
        url = f"http://127.0.0.1:{self.port}/api/state"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic !!!not-base64!!!")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
