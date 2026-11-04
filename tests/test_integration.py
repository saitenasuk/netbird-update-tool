"""真实 SSH 集成测试：需要一个本地 sshd。

准备（脚本化）：
    /tmp/sshd/{host_ed25519,client_key,authorized_keys,sshd_config}
    sshd 监听 127.0.0.1:2222

没准备好的话整组跳过，不影响单测。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbtool.transport import SshTransport  # noqa: E402

SSHD_DIR = Path("/tmp/sshd")


def _sshd_ready() -> bool:
    if os.environ.get("NB_SKIP_SSH_TEST") == "1" or not (SSHD_DIR / "client_key").is_file():
        return False
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 2222), timeout=1):
            return True
    except OSError:
        return False


READY = _sshd_ready()


@unittest.skipUnless(READY, "需要 /tmp/sshd/client_key 与 127.0.0.1:2222 的 sshd")
class SshTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = SshTransport(
            keys_dir=SSHD_DIR, known_hosts=Path(self.tmp.name) / "known_hosts", connect_timeout=5
        )
        self.host = {
            "address": "127.0.0.1",
            "ssh_user": "root",
            "ssh_port": 2222,
            "key_name": "client_key",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_command_and_returns_stdout(self):
        result = self.transport.run(self.host, ["echo", "hello from remote"], timeout=20)
        self.assertTrue(result.ok, result.text)
        self.assertIn("hello from remote", result.text)

    def test_exit_code_propagates(self):
        result = self.transport.run(self.host, ["sh", "-c", "exit 7"], timeout=20)
        self.assertEqual(result.rc, 7)
        self.assertFalse(result.ok)

    def test_streams_lines(self):
        seen = []
        result = self.transport.run(
            self.host, ["sh", "-c", "echo one; echo two; echo three"], timeout=20, on_line=seen.append
        )
        self.assertTrue(result.ok)
        self.assertEqual(seen, ["one", "two", "three"])

    def test_timeout_kills_command(self):
        result = self.transport.run(self.host, ["sleep", "30"], timeout=1.5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_quoting_survives_remote_login_shell(self):
        """远端登录 shell 可能是 fish：shlex.join 拼出来的参数必须原样到达。

        这台机器的 root 登录 shell 就是 /usr/bin/fish，跑这条测试能真正覆盖。
        """
        seen = []
        result = self.transport.run(
            self.host,
            ["printf", "%s\\n", "a b", "c*d", "$HOME", "e;f", "g'h"],
            timeout=20,
            on_line=seen.append,
        )
        self.assertTrue(result.ok, result.text)
        self.assertEqual(seen, ["a b", "c*d", "$HOME", "e;f", "g'h"])

    def test_bad_key_fails_cleanly(self):
        bad = dict(self.host, key_name="does-not-exist")
        result = self.transport.run(bad, ["true"], timeout=10)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.rc, 0)


if __name__ == "__main__":
    unittest.main()
