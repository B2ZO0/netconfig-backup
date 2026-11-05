"""Tests: simulated FortiGate and UniFi APIs, a simulated Cisco SSH server, Git history."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netconfig_backup import core, drivers  # noqa: E402

FORTI_CONF = """#config-version=FGT60F-7.4.4-FW-build2662:opmode=0:vdom=0
#conf_file_ver=123456789
#buildno=2662
config system admin
    edit "admin"
        set password ENC SH2randomsalt{n}==
    next
end
config firewall policy
    edit 1
        set name "LAN to Internet"
    next
end
"""


class FakeApis(BaseHTTPRequestHandler):
    """FortiGate config backup + classic UniFi controller."""
    run = 0
    forti_post_only = False

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/v2/monitor/system/config/backup"):
            if self.headers.get("Authorization") != "Bearer good-token":
                return self._send(401, "{}")
            if FakeApis.forti_post_only:
                return self._send(405, "{}")
            return self._send(200, FORTI_CONF.format(n=FakeApis.run), "text/plain")
        if self.path.startswith("/api/s/default/"):
            if "unifises=ok" not in (self.headers.get("Cookie") or ""):
                return self._send(401, '{"meta":{"rc":"error"}}')
            what = self.path.split("/api/s/default/")[1]
            data = {
                "rest/networkconf": [{"_id": "1", "name": "USERS", "vlan": 10, "x_passphrase": "s3cret"}],
                "rest/wlanconf": [{"_id": "2", "name": "Staff", "x_passphrase": "wifi-password"}],
                "stat/device": [{"name": "USW-24", "mac": "aa:bb", "model": "US24", "uptime": FakeApis.run, "ip": "192.0.2.20"}],
            }.get(what)
            if data is None:
                return self._send(404, "{}")
            return self._send(200, json.dumps({"meta": {"rc": "ok"}, "data": data}))
        self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v2/monitor/system/config/backup":
            return self._send(200, FORTI_CONF.format(n=FakeApis.run), "text/plain")
        if self.path == "/api/auth/login":
            return self._send(404, "{}")  # not UniFi OS: the driver falls back to the classic login
        if self.path == "/api/login":
            if body.get("password") != "good":
                return self._send(400, '{"meta":{"rc":"error","msg":"api.err.Invalid"}}')
            return self._send(200, '{"meta":{"rc":"ok"}}', headers={"Set-Cookie": "unifises=ok; Path=/"})
        self._send(404, "{}")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApis)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeApis.run = 0
        FakeApis.forti_post_only = False
        self.tmp = tempfile.mkdtemp()


class TestDrivers(Base):
    def test_fortigate_masks_secrets_and_volatile_lines(self):
        raw = drivers.fortigate({"host": self.url, "token": "good-token"})
        self.assertNotIn("#conf_file_ver", raw)
        self.assertIn("ENC SH2randomsalt0==", raw)
        masked = drivers.fortigate_mask(raw)
        self.assertIn("set password ENC <redacted>", masked)

    def test_fortigate_falls_back_to_post(self):
        FakeApis.forti_post_only = True
        self.assertIn("config firewall policy", drivers.fortigate({"host": self.url, "token": "good-token"}))

    def test_fortigate_bad_token(self):
        with self.assertRaisesRegex(drivers.BackupError, "401"):
            drivers.fortigate({"host": self.url, "token": "wrong"})

    def test_unifi_export_is_stable_and_redacted(self):
        raw = drivers.unifi({"host": self.url, "username": "backup", "password": "good"})
        FakeApis.run = 99  # uptime changes, configuration does not
        self.assertEqual(raw, drivers.unifi({"host": self.url, "username": "backup", "password": "good"}))
        masked = drivers.unifi_mask(raw)
        self.assertNotIn("wifi-password", masked)
        self.assertIn('"x_passphrase": "<redacted>"', masked)
        self.assertNotIn("uptime", masked)

    def test_unifi_bad_password(self):
        with self.assertRaisesRegex(drivers.BackupError, "login refused"):
            drivers.unifi({"host": self.url, "username": "backup", "password": "bad"})

    def test_cisco_masking(self):
        text = "enable secret 9 $9$abc\nusername admin privilege 15 secret 9 $9$x\nsnmp-server community public RO\n"
        masked = drivers.cisco_mask(text)
        self.assertNotIn("$9$", masked)
        self.assertNotIn("public", masked)


class TestRun(Base):
    def config(self, extra=None):
        p = Path(self.tmp) / "devices.toml"
        p.write_text(f"""
[backup]
repo = "repo"
raw_dir = "raw"

[[device]]
name = "fw01"
type = "fortigate"
host = "{self.url}"
token_env = "TEST_FORTI_TOKEN"

[[device]]
name = "unifi"
type = "unifi"
host = "{self.url}"
username = "backup"
password = "good"

[[device]]
name = "offline"
type = "fortigate"
host = "http://127.0.0.1:1"
token = "x"
timeout = 2
""" + (extra or ""))
        os.environ["TEST_FORTI_TOKEN"] = "good-token"
        return core.load_config(p)

    def test_history_only_records_real_changes(self):
        cfg = self.config()
        res, commit = core.run(cfg)
        st = {r.name: r.status for r in res}
        self.assertEqual(st, {"fw01": "new", "unifi": "new", "offline": "failed"})
        self.assertTrue(commit)
        # second run: FortiGate re-encrypted its secrets, UniFi uptime moved: no real change
        FakeApis.run = 1
        res, commit2 = core.run(cfg)
        self.assertEqual({r.name: r.status for r in res}, {"fw01": "unchanged", "unifi": "unchanged", "offline": "failed"})
        self.assertEqual(commit2, "")
        log = core.git(cfg["backup"]["repo"], "log", "--oneline")
        self.assertEqual(len(log.splitlines()), 1)
        # raw copies keep the real secrets for restores, with private permissions
        raws = sorted((Path(self.tmp) / "raw" / "fw01").iterdir(), key=lambda f: f.stat().st_mtime_ns)
        self.assertEqual(len(raws), 2)
        self.assertIn("SH2randomsalt0", raws[0].read_text())
        self.assertEqual(oct(raws[0].stat().st_mode & 0o777), "0o600")

    def test_missing_secret_env_is_a_clear_error(self):
        cfg = self.config()
        del os.environ["TEST_FORTI_TOKEN"]
        res, _ = core.run(cfg)
        fw = next(r for r in res if r.name == "fw01")
        self.assertEqual(fw.status, "failed")
        self.assertIn("TEST_FORTI_TOKEN", fw.error)

    def test_config_errors(self):
        p = Path(self.tmp) / "bad.toml"
        p.write_text('[[device]]\nname = "x y"\ntype = "cisco_ios"\nhost = "h"\n')
        with self.assertRaisesRegex(core.ConfigError, "may only contain"):
            core.load_config(p)
        p.write_text('[[device]]\nname = "x"\ntype = "juniper"\nhost = "h"\n')
        with self.assertRaisesRegex(core.ConfigError, "unknown type"):
            core.load_config(p)

    def test_demo_command(self):
        out = subprocess.run([sys.executable, "-m", "netconfig_backup", "demo", str(Path(self.tmp) / "demo")],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("+vlan 30", out.stdout)


try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipIf(paramiko is None, "paramiko not installed")
class TestCiscoSsh(unittest.TestCase):
    """A tiny SSH server that behaves like a Cisco IOS switch."""

    CONFIG = ("Building configuration...\r\n\r\nCurrent configuration : 1234 bytes\r\n!\r\n"
              "! Last configuration change at 10:00:00 UTC Mon Nov 2 2026\r\n!\r\nhostname sw-test\r\n"
              "enable secret 9 $9$abcdef\r\n!\r\ninterface GigabitEthernet1/0/1\r\n description UPLINK\r\n!\r\nend\r\n")

    def start_server(self, key, port=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        threading.Thread(target=self.serve, args=(sock, key), daemon=True).start()
        return sock.getsockname()[1]

    def serve(self, sock, key):
        conn, _ = sock.accept()
        sock.close()
        t = paramiko.Transport(conn)
        t.add_server_key(key)
        outer = self

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if (username, password) == ("backup", "good") else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED

            def check_channel_shell_request(self, channel):
                return True

            def check_channel_pty_request(self, *a):
                return True

        try:
            t.start_server(server=Server())
        except Exception:  # noqa: BLE001 - the client may refuse the key
            return
        chan = t.accept(10)
        if chan is None:
            return
        chan.send(b"\r\nsw-test#")
        buf = b""
        while True:
            try:
                data = chan.recv(1024)
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.strip().decode()
                chan.send(cmd.encode() + b"\r\n")
                if cmd == "show running-config":
                    chan.send(outer.CONFIG.encode())
                chan.send(b"sw-test#")

    def dev(self, port, known):
        return {"host": "127.0.0.1", "port": port, "username": "backup", "password": "good", "timeout": 10,
                "_known_hosts": known}

    def test_cisco_backup(self):
        known = str(Path(tempfile.mkdtemp()) / "known_hosts")
        port = self.start_server(paramiko.RSAKey.generate(2048))
        text = drivers.cisco_ios(self.dev(port, known))
        self.assertIn("hostname sw-test", text.splitlines()[:3], text)
        self.assertNotIn("Last configuration change", text)
        self.assertNotIn("Current configuration", text)
        self.assertNotIn("sw-test#", text)
        self.assertIn("enable secret 9 <redacted>", drivers.cisco_mask(text))
        self.assertIn("127.0.0.1", Path(known).read_text())  # key remembered at first connection

    def test_changed_host_key_is_refused(self):
        known = str(Path(tempfile.mkdtemp()) / "known_hosts")
        port = self.start_server(paramiko.RSAKey.generate(2048))
        drivers.cisco_ios(self.dev(port, known))
        self.start_server(paramiko.RSAKey.generate(2048), port)  # same address, another key
        with self.assertRaisesRegex(drivers.BackupError, "CHANGED"):
            drivers.cisco_ios(self.dev(port, known))

    def test_strict_refuses_unknown_keys(self):
        known = str(Path(tempfile.mkdtemp()) / "known_hosts")
        port = self.start_server(paramiko.RSAKey.generate(2048))
        with self.assertRaisesRegex(drivers.BackupError, "unknown SSH host key"):
            drivers.cisco_ios({**self.dev(port, known), "host_key": "strict"})


class TestSafety(Base):
    def test_folder_cannot_leave_the_repository(self):
        p = Path(self.tmp) / "devices.toml"
        p.write_text(f'[backup]\nrepo = "repo"\n[[device]]\nname = "fw"\ntype = "fortigate"\nhost = "{self.url}"\n'
                     f'token = "good-token"\nfolder = "../../escape"\n')
        res, _ = core.run(core.load_config(p))
        self.assertEqual(res[0].status, "failed")
        self.assertIn("leaves the backup repository", res[0].error)
        self.assertFalse((Path(self.tmp).parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
