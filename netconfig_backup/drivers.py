"""Device drivers: each one returns the configuration of a device as text."""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_KEY = re.compile(r"(pass|secret|psk|key|token|community|x_)", re.I)
REDACTED = "<redacted>"


class BackupError(Exception):
    """Raised when a device cannot be backed up; the message is shown to the user."""


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------
def _ssl_context(dev: dict) -> ssl.SSLContext:
    if dev.get("ca_file"):
        return ssl.create_default_context(cafile=dev["ca_file"])
    ctx = ssl.create_default_context()
    if not dev.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _base_url(dev: dict, default_scheme: str = "https") -> str:
    host = dev["host"]
    if "://" not in host:
        host = f"{default_scheme}://{host}"
    if dev.get("port"):
        p = urllib.parse.urlsplit(host)
        host = f"{p.scheme}://{p.hostname}:{dev['port']}"
    return host.rstrip("/")


def _redact(obj):
    """Recursively replaces values whose key looks like a secret."""
    if isinstance(obj, dict):
        return {k: (REDACTED if SECRET_KEY.search(k) and isinstance(v, (str, int)) and v != "" else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _strip_lines(text: str, patterns: list[str]) -> str:
    regs = [re.compile(p) for p in patterns]
    out = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
           if not any(r.search(line) for r in regs)]
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------
# SSH (Cisco IOS and generic CLI devices)
# ------------------------------------------------------------------------------------------
CISCO_VOLATILE = [
    r"^Building configuration",
    r"^Current configuration\s*:",
    r"^! Last configuration change",
    r"^! NVRAM config last updated",
    r"^! No configuration change since last restart",
    r"^ntp clock-period",
]
CISCO_SECRETS = [
    (re.compile(r"^(\s*(?:enable )?(?:secret|password)\s+\d+\s+)\S+", re.M), r"\1" + REDACTED),
    (re.compile(r"^(\s*username\s+\S+.*?\b(?:secret|password)\s+\d+\s+)\S+", re.M), r"\1" + REDACTED),
    (re.compile(r"^(\s*snmp-server community\s+)\S+", re.M), r"\1" + REDACTED),
    (re.compile(r"^(\s*(?:key|key-string|pre-shared-key|tacacs-server key|radius-server key)\s+(?:\d+\s+)?)\S+", re.M), r"\1" + REDACTED),
]


def _ssh_run(dev: dict, commands: list[str]) -> str:
    try:
        import paramiko  # imported here: only SSH devices need it
    except ImportError as e:
        raise BackupError("SSH devices need paramiko: pip install paramiko") from e
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(dev["host"], port=int(dev.get("port", 22)), username=dev["username"],
                       password=dev.get("password"), key_filename=dev.get("key_file"),
                       timeout=float(dev.get("timeout", 20)), look_for_keys=bool(dev.get("key_file")),
                       allow_agent=False)
        chan = client.invoke_shell(width=511, height=1000)
        chan.settimeout(float(dev.get("timeout", 20)))
        prompt_end = re.compile(dev.get("prompt", r"[>#]\s*$").encode())
        buf = b""

        def read_until_prompt(deadline: float) -> bytes:
            data = b""
            while time.time() < deadline:
                if chan.recv_ready():
                    data += chan.recv(65535)
                    if prompt_end.search(data):
                        time.sleep(0.2)
                        if not chan.recv_ready():
                            return data
                else:
                    time.sleep(0.1)
            return data

        read_until_prompt(time.time() + 10)  # banner and first prompt
        for cmd in commands:
            chan.send((cmd + "\n").encode())
            buf += read_until_prompt(time.time() + float(dev.get("timeout", 60)))
        out = buf.decode("utf-8", "replace")
    except BackupError:
        raise
    except Exception as e:  # noqa: BLE001 - shown to the user
        raise BackupError(f"SSH: {e}") from e
    finally:
        client.close()
    # keep what is between the last command echo and the final prompt
    last = commands[-1]
    i = out.rfind(last)
    if i >= 0:
        out = out[i + len(last):]
    lines = out.replace("\r", "").split("\n")
    if lines and re.search(r"[>#]\s*$", lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n") + "\n"


def cisco_ios(dev: dict) -> str:
    cmds = ["terminal length 0", "terminal width 511", dev.get("command", "show running-config")]
    raw = _ssh_run(dev, cmds)
    if re.match(r"\s*%", raw):
        raise BackupError("the device refused the command: " + raw.strip().splitlines()[0][:120])
    return _strip_lines(raw, CISCO_VOLATILE + dev.get("ignore", []))


def cisco_mask(text: str) -> str:
    for rx, rep in CISCO_SECRETS:
        text = rx.sub(rep, text)
    return text


def ssh_generic(dev: dict) -> str:
    cmds = dev.get("pre_commands", []) + [dev["command"]]
    return _strip_lines(_ssh_run(dev, cmds), dev.get("ignore", []))


# ------------------------------------------------------------------------------------------
# FortiGate (REST API, read-only token)
# ------------------------------------------------------------------------------------------
FORTI_VOLATILE = [r"^#conf_file_ver=", r"^#buildno=", r"^#global_vdom="]
FORTI_ENC = re.compile(r"(\bENC\s+)\S+")


def fortigate(dev: dict) -> str:
    base = _base_url(dev)
    ctx = _ssl_context(dev)
    headers = {"Authorization": f"Bearer {dev['token']}", "Accept": "text/plain, */*"}
    scope = dev.get("scope", "global")
    attempts = [
        urllib.request.Request(f"{base}/api/v2/monitor/system/config/backup?scope={scope}", headers=headers),
        # FortiOS 7.6+: POST with a JSON body
        urllib.request.Request(f"{base}/api/v2/monitor/system/config/backup", method="POST",
                               data=json.dumps({"scope": scope}).encode(),
                               headers={**headers, "Content-Type": "application/json"}),
    ]
    last_err = None
    for req in attempts:
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=float(dev.get("timeout", 30))) as r:
                text = r.read().decode("utf-8", "replace")
            if not text.lstrip().startswith(("#config-version", "config ")):
                raise BackupError("unexpected answer (is the token allowed to read the configuration?)")
            return _strip_lines(text, FORTI_VOLATILE + dev.get("ignore", []))
        except urllib.error.HTTPError as e:
            last_err = BackupError(f"HTTP {e.code} {e.reason}")
            if e.code in (401, 403):
                raise BackupError(f"HTTP {e.code}: check the API token and its trusted hosts") from e
        except urllib.error.URLError as e:
            raise BackupError(f"cannot reach {base}: {e.reason}") from e
    raise last_err or BackupError("backup failed")


# ------------------------------------------------------------------------------------------
# UniFi Network (classic controller or UniFi OS console)
# ------------------------------------------------------------------------------------------
UNIFI_ENDPOINTS = {
    "networks": "rest/networkconf",
    "wifi": "rest/wlanconf",
    "port_profiles": "rest/portconf",
    "firewall_rules": "rest/firewallrule",
    "firewall_groups": "rest/firewallgroup",
    "routes": "rest/routing",
    "settings": "rest/setting",
    "devices": "stat/device",
}
UNIFI_DEVICE_KEYS = ["name", "mac", "model", "type", "ip", "version", "config_network", "port_overrides",
                     "radio_table", "mgmt_network_id", "stp_priority", "stp_version"]
UNIFI_VOLATILE_KEYS = {"_id", "site_id", "last_seen", "uptime", "state", "adopted_at", "time", "x_ssh_hostkey_fingerprint"}


def _unifi_clean(obj):
    if isinstance(obj, dict):
        return {k: _unifi_clean(v) for k, v in sorted(obj.items()) if k not in UNIFI_VOLATILE_KEYS}
    if isinstance(obj, list):
        items = [_unifi_clean(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True)) if all(isinstance(x, dict) for x in items) else items
    return obj


def unifi(dev: dict) -> str:
    base = _base_url(dev)
    ctx = _ssl_context(dev)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=ctx))
    site = dev.get("site", "default")
    timeout = float(dev.get("timeout", 30))
    body = json.dumps({"username": dev["username"], "password": dev["password"]}).encode()
    csrf = None
    prefix = ""
    try:  # UniFi OS (Cloud Key Gen2, UDM, UCG...)
        r = opener.open(urllib.request.Request(f"{base}/api/auth/login", data=body, headers={"Content-Type": "application/json"}), timeout=timeout)
        csrf = r.headers.get("X-CSRF-Token")
        prefix = "/proxy/network"
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            raise BackupError(f"UniFi login refused (HTTP {e.code}): check the account") from e
        try:  # classic controller
            opener.open(urllib.request.Request(f"{base}/api/login", data=body, headers={"Content-Type": "application/json"}), timeout=timeout)
        except urllib.error.HTTPError as e2:
            raise BackupError(f"UniFi login refused (HTTP {e2.code}): check the account") from e2
    except urllib.error.URLError as e:
        raise BackupError(f"cannot reach {base}: {e.reason}") from e

    export = {}
    for name, path in UNIFI_ENDPOINTS.items():
        headers = {"Accept": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        try:
            with opener.open(urllib.request.Request(f"{base}{prefix}/api/s/{site}/{path}", headers=headers), timeout=timeout) as r:
                data = json.loads(r.read().decode()).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise BackupError(f"UniFi {path}: HTTP {e.code}") from e
        if name == "devices":
            data = [{k: d[k] for k in UNIFI_DEVICE_KEYS if k in d} for d in data]
        export[name] = data
    return json.dumps(_unifi_clean(export), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def fortigate_mask(text: str) -> str:
    # FortiOS re-encrypts secrets with a new salt at every export: masking them
    # also keeps Git from showing fake changes. Use raw_dir to keep restorable copies.
    return FORTI_ENC.sub(r"\1" + REDACTED, text)


def unifi_mask(text: str) -> str:
    return json.dumps(_redact(json.loads(text)), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# name -> (fetch, mask secrets, file extension)
DRIVERS = {
    "cisco_ios": (cisco_ios, cisco_mask, ".cfg"),
    "ssh": (ssh_generic, None, ".cfg"),
    "fortigate": (fortigate, fortigate_mask, ".conf"),
    "unifi": (unifi, unifi_mask, ".json"),
}
