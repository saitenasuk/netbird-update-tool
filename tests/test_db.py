"""SQLite 层与地址分类测试。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbtool import netaddr  # noqa: E402
from nbtool.db import Store, ValidationError, parse_scripts  # noqa: E402


class ParseScriptsTest(unittest.TestCase):
    def test_parses_path_label_options(self):
        scripts = parse_scripts(
            "/root/a.sh | 更新控制台 | management,dashboard\n"
            "# 注释行\n"
            "\n"
            "/root/b.sh\n"
        )
        self.assertEqual(len(scripts), 2)
        self.assertEqual(scripts[0]["path"], "/root/a.sh")
        self.assertEqual(scripts[0]["label"], "更新控制台")
        self.assertEqual(scripts[0]["options"], ["management", "dashboard"])
        self.assertEqual(scripts[1]["label"], "b.sh", "没写显示名就用文件名")
        self.assertEqual(scripts[1]["options"], [])

    def test_requires_absolute_path(self):
        with self.assertRaises(ValidationError):
            parse_scripts("relative.sh")

    def test_rejects_bad_option_name(self):
        with self.assertRaises(ValidationError):
            parse_scripts("/root/a.sh | x | bad option")

    def test_rejects_empty(self):
        with self.assertRaises(ValidationError):
            parse_scripts("   \n\n")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db.sqlite3"))
        self.payload = {
            "name": "aliyun",
            "address": "10.0.0.1",
            "ssh_user": "root",
            "ssh_port": 22,
            "group_name": "console",
            "key_name": None,
            "scripts": "/root/a.sh | 控制台 | management,dashboard\n/root/b.sh | 客户端 |",
            "detach": False,
            "detach_log": "/var/log/x.log",
            "self_host": False,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, **over):
        payload = dict(self.payload)
        payload.update(over)
        payload["scripts_parsed"] = parse_scripts(payload["scripts"])
        return self.store.create_host(payload)

    def test_create_and_read_back(self):
        host_id = self._create()
        host = self.store.get_host(host_id)
        self.assertEqual(host["name"], "aliyun")
        self.assertEqual(len(host["scripts"]), 2)
        self.assertEqual(host["scripts"][0]["options"], ["management", "dashboard"])

    def test_duplicate_name_rejected(self):
        self._create()
        with self.assertRaises(Exception):
            self._create()

    def test_update_replaces_scripts(self):
        host_id = self._create()
        payload = dict(self.payload)
        payload["scripts"] = "/root/only.sh | 只有一个 |"
        payload["scripts_parsed"] = parse_scripts(payload["scripts"])
        self.store.update_host(host_id, payload)
        host = self.store.get_host(host_id)
        self.assertEqual([s["path"] for s in host["scripts"]], ["/root/only.sh"])

    def test_delete_cascades_scripts(self):
        host_id = self._create()
        self.store.delete_host(host_id)
        count = self.store.conn.execute("SELECT COUNT(*) FROM scripts").fetchone()[0]
        self.assertEqual(count, 0)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self._create(name="有 空格")
        with self.assertRaises(ValidationError):
            self._create(address="")
        with self.assertRaises(ValidationError):
            self._create(ssh_port=99999)

    def test_jobs_roundtrip_and_prune(self):
        for i in range(5):
            self.store.save_job(f"job-{i}", "success", {"created_at": f"2026-11-0{i + 1}T00:00:00+08:00", "label": "x"}, None)
        self.assertEqual(len(self.store.list_jobs()), 5)
        self.store.prune_jobs(2)
        self.assertEqual(len(self.store.list_jobs()), 2)
        self.assertTrue(self.store.get_job("job-4"))


class NetAddrTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(netaddr.classify("100.64.0.7"), "netbird")
        self.assertEqual(netaddr.classify("192.168.1.10"), "lan")
        self.assertEqual(netaddr.classify("10.1.2.3"), "lan")
        self.assertEqual(netaddr.classify("8.8.8.8"), "public")
        self.assertEqual(netaddr.classify("127.0.0.1"), "loopback")
        self.assertEqual(netaddr.classify("nashira.local"), "hostname")

    def test_netbird_address_warns(self):
        info = netaddr.describe("100.64.0.7", set())
        self.assertEqual(info["kind"], "netbird")
        self.assertTrue(info["warn"], "netbird 地址必须给出断线警告")

    def test_lan_address_has_no_warning(self):
        self.assertEqual(netaddr.describe("192.168.1.10", set())["warn"], "")


if __name__ == "__main__":
    unittest.main()
