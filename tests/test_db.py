"""SQLite 层与地址分类测试。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbtool import netaddr  # noqa: E402
from nbtool.keys import KeyError_ as KeyNameError  # noqa: E402
from nbtool.keys import validate_name  # noqa: E402
from nbtool.db import Store, ValidationError, parse_scripts  # noqa: E402


class ParseScriptsTest(unittest.TestCase):
    def test_parses_command_label_args(self):
        scripts = parse_scripts(
            "/root/a.sh | 更新控制台 | management dashboard\n"
            "# 注释行\n"
            "\n"
            "/root/b.sh\n"
        )
        self.assertEqual(len(scripts), 2)
        self.assertEqual(scripts[0]["path"], "/root/a.sh")
        self.assertEqual(scripts[0]["label"], "更新控制台")
        self.assertEqual(scripts[0]["args"], ["management", "dashboard"])
        self.assertEqual(scripts[1]["label"], "b.sh", "没写显示名就用文件名")
        self.assertEqual(scripts[1]["args"], [])

    def test_args_support_quotes(self):
        scripts = parse_scripts('/root/a.sh | x | --tag "v1 2" --force')
        self.assertEqual(scripts[0]["args"], ["--tag", "v1 2", "--force"])

    def test_requires_absolute_path(self):
        with self.assertRaises(ValidationError):
            parse_scripts("relative.sh")

    def test_rejects_unbalanced_quotes(self):
        with self.assertRaises(ValidationError):
            parse_scripts('/root/a.sh | x | "unterminated')

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
            "scripts": "/root/a.sh | 控制台 | management dashboard\n/root/b.sh | 客户端 |",
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
        self.assertEqual(host["scripts"][0]["args"], ["management", "dashboard"])

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


class NameValidationTest(unittest.TestCase):
    """名字允许中文，但挡掉路径分隔符和控制字符。"""

    def test_chinese_names_allowed(self):
        self.assertEqual(validate_name("我的密钥"), "我的密钥")
        self.assertEqual(validate_name("  测试密钥  "), "测试密钥")

    def test_rejects_path_and_control_chars(self):
        for bad in ["", "a/b", "a\\b", "a:b", "a b", ".hidden", "..", "a" * 65]:
            with self.assertRaises(KeyNameError, msg=repr(bad)):
                validate_name(bad)


class SudoColumnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "db.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_database_gets_column(self):
        """老库没有 sudo 列，打开后要自动补上。"""
        import sqlite3

        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                group_name TEXT NOT NULL DEFAULT 'clients',
                address TEXT NOT NULL,
                ssh_user TEXT NOT NULL DEFAULT 'root',
                ssh_port INTEGER NOT NULL DEFAULT 22,
                key_name TEXT,
                detach INTEGER NOT NULL DEFAULT 0,
                detach_log TEXT NOT NULL DEFAULT '/var/log/netbird-upgrade.log',
                detach_unit_pattern TEXT NOT NULL DEFAULT 'netbird-upgrade-*',
                self_host INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            INSERT INTO hosts (name, address, created_at) VALUES ('old', '10.0.0.1', '2026-01-01');
            """
        )
        conn.commit()
        conn.close()

        store = Store(self.path)  # 打开时迁移
        host = store.list_hosts()[0]
        self.assertEqual(host["name"], "old")
        self.assertFalse(host["sudo"], "老数据默认不带 sudo")
        self.assertIn("sudo", {r[1] for r in store.conn.execute("PRAGMA table_info(hosts)")})

    def test_check_result_persists(self):
        store = Store(self.path)
        payload = {
            "name": "h1",
            "address": "10.0.0.3",
            "key_name": None,
            "scripts": "/root/x.sh | 更新 |",
        }
        payload["scripts_parsed"] = parse_scripts(payload["scripts"])
        host_id = store.create_host(payload)
        self.assertEqual(store.get_host(host_id)["check"], {})

        store.set_check(host_id, {"at": "2026-11-03T20:00:00+08:00", "reachable": True, "version": "0.67.0"})
        store2 = Store(self.path)  # 重新打开，模拟进程重启
        self.assertEqual(store2.get_host(host_id)["check"]["version"], "0.67.0")

    def test_sudo_roundtrip(self):
        store = Store(self.path)
        payload = {
            "name": "oci",
            "address": "10.0.0.2",
            "ssh_user": "ubuntu",
            "key_name": None,
            "scripts": "/usr/local/bin/update | 更新客户端 |",
            "sudo": True,
        }
        payload["scripts_parsed"] = parse_scripts(payload["scripts"])
        host_id = store.create_host(payload)
        self.assertTrue(store.get_host(host_id)["sudo"])

        payload["scripts_parsed"] = parse_scripts(payload["scripts"])
        payload["sudo"] = False
        store.update_host(host_id, payload)
        self.assertFalse(store.get_host(host_id)["sudo"])


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
