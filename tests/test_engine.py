"""引擎行为测试：排序、门禁、容错、前置检查、参数、detach 等待。

全部用 FakeTransport，不需要真机器。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbtool.engine import Engine, Settings  # noqa: E402
from nbtool.transport import FakeTransport, Result  # noqa: E402


def mkhost(host_id, name, group="clients", self_host=False, detach=False, address="10.0.0.1"):
    return {
        "id": host_id,
        "name": name,
        "group_name": group,
        "address": address,
        "ssh_user": "root",
        "ssh_port": 22,
        "key_name": "k",
        "detach": detach,
        "detach_log": "/var/log/netbird-upgrade.log",
        "detach_unit_pattern": "netbird-upgrade-*",
        "self_host": self_host,
        "sort_order": 0,
    }


def mkscript(path, args=None):
    return {"path": path, "args": args or [], "timeout": None}


def base_responses(ok_scripts=(), fail_scripts=()):
    responses = {("true",): Result(0), ("netbird", "version"): Result(0, ["0.61.0"])}
    for path in ok_scripts:
        responses[(path,)] = Result(0, [f"{path} ok"])
    for path in fail_scripts:
        responses[(path,)] = Result(1, [f"{path}炸了"])
    return responses


class EngineOrderTest(unittest.TestCase):
    def test_console_first_then_clients_self_last(self):
        engine = Engine(FakeTransport())
        hosts = [
            mkhost(5, "c-late", "clients"),
            mkhost(1, "console", "console"),
            mkhost(6, "c-self", "clients", self_host=True),
            mkhost(2, "c-early", "clients"),
        ]
        ordered = [h["name"] for h in engine.order(hosts)]
        self.assertEqual(ordered, ["console", "c-early", "c-late", "c-self"])
        self.assertEqual([h["name"] for h in engine.order(hosts)], ordered, "排序应稳定")


class EngineGatingTest(unittest.TestCase):
    def test_console_failure_aborts_rest(self):
        fake = FakeTransport(base_responses(ok_scripts=["/bin/true"], fail_scripts=["/bin/false"]))
        fake.responses[("test", "-x", "/bin/false")] = Result(0)
        fake.responses[("test", "-x", "/bin/true")] = Result(0)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [
            {"host": mkhost(1, "console", "console"), "scripts": [mkscript("/bin/false")]},
            {"host": mkhost(2, "client", "clients"), "scripts": [mkscript("/bin/true")]},
        ]
        lines = []
        result = engine.run(targets, lambda h, t, k="info": lines.append((h, t, k)))

        self.assertEqual(result["status"], "aborted")
        self.assertEqual(len(result["hosts"]), 1, "控制台失败后不应再处理客户端")
        self.assertEqual(result["hosts"][0]["status"], "failed")
        self.assertFalse(fake.called("/bin/true"), "客户端的脚本绝不能被调用")
        self.assertTrue(any("任务中止" in t for _h, t, _k in lines))

    def test_client_failure_does_not_stop_others(self):
        fake = FakeTransport(base_responses(ok_scripts=["/bin/true"], fail_scripts=["/bin/false"]))
        fake.responses[("test", "-x", "/bin/true")] = Result(0)
        fake.responses[("test", "-x", "/bin/false")] = Result(0)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [
            {"host": mkhost(1, "a", "clients"), "scripts": [mkscript("/bin/false")]},
            {"host": mkhost(2, "b", "clients"), "scripts": [mkscript("/bin/true")]},
        ]
        result = engine.run(targets, lambda *a, **k: None)

        self.assertEqual(result["status"], "failed")
        self.assertEqual([h["status"] for h in result["hosts"]], ["failed", "ok"])
        self.assertTrue(fake.called("/bin/true"))


class EnginePreflightTest(unittest.TestCase):
    def test_missing_script_skips_host_before_running(self):
        fake = FakeTransport(base_responses(ok_scripts=["/root/x.sh"]))
        fake.responses[("test", "-x", "/root/x.sh")] = Result(1)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [{"host": mkhost(1, "a"), "scripts": [mkscript("/root/x.sh")]}]
        result = engine.run(targets, lambda *a, **k: None)

        self.assertEqual(result["hosts"][0]["status"], "failed")
        self.assertIn("/root/x.sh", result["hosts"][0]["error"])
        self.assertFalse(fake.called("/root/x.sh"), "前置检查失败就不该执行脚本")

    def test_args_are_passed_through(self):
        fake = FakeTransport(base_responses(ok_scripts=["/root/c.sh"]))
        fake.responses[("test", "-x", "/root/c.sh")] = Result(0)
        fake.responses[("/root/c.sh", "management", "dashboard")] = Result(0)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [
            {
                "host": mkhost(1, "a"),
                "scripts": [mkscript("/root/c.sh", ["management", "dashboard"])],
            }
        ]
        result = engine.run(targets, lambda *a, **k: None)
        self.assertEqual(result["status"], "success")
        self.assertTrue(fake.called("/root/c.sh", "management", "dashboard"))

    def test_unreachable_host_fails_but_others_run(self):
        fake = FakeTransport()
        fake.responses[("true",)] = lambda host, argv: (
            Result(1, ["Connection refused"]) if host["address"] == "10.0.0.9" else Result(0)
        )
        fake.responses[("test", "-x", "/root/x.sh")] = Result(0)
        fake.responses[("/root/x.sh",)] = Result(0)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [
            {"host": mkhost(1, "dead", address="10.0.0.9"), "scripts": [mkscript("/root/x.sh")]},
            {"host": mkhost(2, "alive"), "scripts": [mkscript("/root/x.sh")]},
        ]
        result = engine.run(targets, lambda *a, **k: None)
        self.assertEqual([h["status"] for h in result["hosts"]], ["failed", "ok"])
        self.assertIn("连不上", result["hosts"][0]["error"])


class EngineDetachTest(unittest.TestCase):
    def test_waits_for_reconnect_and_unit_then_reads_new_version(self):
        pings = {"n": 0}
        versions = {"n": 0}
        units = {"n": 0}

        def ping(host, argv):
            pings["n"] += 1
            # 第 1 次是连通性检查；之后模拟断线两次再恢复
            return Result(0) if pings["n"] in (1, 4, 5) else Result(255, ["Connection timed out"])

        def version(host, argv):
            versions["n"] += 1
            return Result(0, ["0.67.0" if versions["n"] > 1 else "0.61.0"])

        def unit(host, argv):
            units["n"] += 1
            return Result(0, ["netbird-upgrade-1.service loaded active running"]) if units["n"] < 3 else Result(0, [])

        fake = FakeTransport()
        fake.responses[("true",)] = ping
        fake.responses[("netbird", "version")] = version
        fake.responses[("systemctl", "list-units", "--all", "--plain", "--no-legend", "netbird-upgrade-*")] = unit
        fake.responses[("test", "-x", "/root/up.sh")] = Result(0)
        fake.responses[("/root/up.sh",)] = Result(0, ["已调度后台升级任务"])
        fake.responses[("tail", "-n", "20", "/var/log/netbird-upgrade.log")] = Result(0, ["===== done ====="])

        engine = Engine(fake, sleep=lambda _s: None)
        targets = [{"host": mkhost(1, "subnet", detach=True), "scripts": [mkscript("/root/up.sh")]}]
        lines = []
        result = engine.run(targets, lambda h, t, k="info": lines.append(t))

        host = result["hosts"][0]
        self.assertEqual(host["status"], "ok", host["error"])
        self.assertEqual(host["version_before"], "0.61.0")
        self.assertEqual(host["version_after"], "0.67.0")
        self.assertTrue(any("等待 NetBird 断线重连" in t for t in lines))
        self.assertTrue(any("===== done =====" in t for t in lines))

    def test_unit_never_finishes_times_out(self):
        fake = FakeTransport()
        fake.responses[("test", "-x", "/root/up.sh")] = Result(0)
        fake.responses[("/root/up.sh",)] = Result(0)
        fake.responses[("systemctl", "list-units", "--all", "--plain", "--no-legend", "netbird-upgrade-*")] = Result(
            0, ["netbird-upgrade-1.service loaded active running"]
        )
        engine = Engine(fake, settings=Settings(detach_poll_timeout=0), sleep=lambda _s: None)
        targets = [{"host": mkhost(1, "subnet", detach=True), "scripts": [mkscript("/root/up.sh")]}]
        result = engine.run(targets, lambda *a, **k: None)
        self.assertEqual(result["hosts"][0]["status"], "failed")
        self.assertIn("后台升级单元", result["hosts"][0]["error"])

    def test_timeout_marks_failure(self):
        fake = FakeTransport()
        fake.responses[("test", "-x", "/root/up.sh")] = Result(0)
        fake.responses[("/root/up.sh",)] = Result(0, lines=[], timed_out=True)
        engine = Engine(fake, sleep=lambda _s: None)
        targets = [{"host": mkhost(1, "a"), "scripts": [mkscript("/root/up.sh")]}]
        result = engine.run(targets, lambda *a, **k: None)
        self.assertEqual(result["hosts"][0]["status"], "failed")
        self.assertIn("超时", result["hosts"][0]["error"])


if __name__ == "__main__":
    unittest.main()
