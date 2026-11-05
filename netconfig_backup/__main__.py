"""Command line: python -m netconfig_backup [run|test|history|show|demo] ..."""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, core
from .drivers import BackupError


def cmd_run(args) -> int:
    cfg = core.load_config(args.config)
    if args.device:
        cfg["devices"] = [d for d in cfg["devices"] if d["name"] in args.device]
        if not cfg["devices"]:
            raise core.ConfigError("none of the requested devices is in the configuration")
    results, commit = core.run(cfg)
    core.print_report(cfg, results, commit)
    core.notify(cfg, results, commit)
    return 2 if any(r.status == "failed" for r in results) else 0


def cmd_test(args) -> int:
    """Connects to every device and reads its configuration, without writing anything."""
    cfg = core.load_config(args.config)
    failed = 0
    for dev in cfg["devices"]:
        try:
            text, _ = core._fetch(dev)
            print(f"  {core.color('✔', '32')} {dev['name']}: {len(text.splitlines())} lines")
        except (BackupError, core.ConfigError) as e:
            failed += 1
            print(f"  {core.color('✘', '31')} {dev['name']}: {e}")
    return 2 if failed else 0


def cmd_history(args) -> int:
    cfg = core.load_config(args.config)
    paths = [p.name for p in Path(cfg["backup"]["repo"]).rglob(f"{args.device}.*") if ".git" not in p.parts] if args.device else []
    out = core.git(cfg["backup"]["repo"], "log", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h  %ad  %s",
                   f"-n{args.n}", "--", *paths)
    print(out or "No history yet.")
    return 0


def cmd_show(args) -> int:
    cfg = core.load_config(args.config)
    repo = cfg["backup"]["repo"]
    paths = [str(p.relative_to(repo)) for p in Path(repo).rglob(f"{args.device}.*") if ".git" not in p.parts] if args.device else []
    color = "--color=always" if core.COLORS else "--color=never"
    print(core.git(repo, "show", color, "--stat", "--patch", "--format=%C(yellow)%h%Creset  %ad  %s", "--date=format:%Y-%m-%d %H:%M",
                   args.commit, "--", *paths))
    return 0


# ------------------------------------------------------------------------------------------
# Demo: three simulated backups of a small network, with real Git history
# ------------------------------------------------------------------------------------------
DEMO_SWITCH = """hostname {name}
!
vlan 10
 name USERS
vlan 20
 name SERVERS
{extra_vlan}!
interface GigabitEthernet1/0/1
 description {uplink}
 switchport mode trunk
!
interface GigabitEthernet1/0/2
 description PC-ACCOUNTING
 switchport access vlan 10
 spanning-tree portfast
!
snmp-server community <redacted> RO
ntp server 192.0.2.123
end
"""
DEMO_FW = """#config-version=FGT60F-7.4.4-FW-build2662:opmode=0:vdom=0
config system global
    set hostname "fw01"
    set timezone "Europe/Paris"
end
config firewall policy
    edit 1
        set name "LAN to Internet"
        set srcintf "lan"
        set dstintf "wan1"
        set action accept
        set service "ALL"
    next
{extra_policy}end
"""


def cmd_demo(args) -> int:
    folder = Path(args.folder or tempfile.mkdtemp(prefix="netconfig-backup-demo-"))
    if folder.exists() and any(folder.iterdir()) and not args.force:
        print(f"{folder} is not empty (use --force)")
        return 1
    shutil.rmtree(folder, ignore_errors=True)
    state = {"run": 0}

    def source(dev):
        n = state["run"]
        if dev["name"] == "unifi" and n == 2:
            raise BackupError("cannot reach https://192.0.2.5:8443: timed out")
        if dev["name"] == "fw01":
            extra = ('    edit 2\n        set name "Guests to Internet"\n        set srcintf "guest"\n'
                     '        set dstintf "wan1"\n        set action accept\n        set service "HTTP" "HTTPS" "DNS"\n    next\n') if n >= 1 else ""
            return DEMO_FW.format(extra_policy=extra)
        if dev["name"] == "unifi":
            return '{\n  "networks": [\n    {"name": "USERS", "vlan": 10},\n    {"name": "SERVERS", "vlan": 20}\n  ]\n}\n'
        extra = "vlan 30\n name VOIP\n" if (dev["name"] == "sw-floor1" and n >= 2) else ""
        uplink = "UPLINK sw-core" if dev["name"] != "sw-core" else "UPLINK fw01"
        return DEMO_SWITCH.format(name=dev["name"], extra_vlan=extra, uplink=uplink)

    cfg = {"backup": {"repo": str(folder)}, "notify": {}, "devices": [
        {"name": "sw-core", "type": "demo", "label": "cisco_ios", "extension": ".cfg"},
        {"name": "sw-floor1", "type": "demo", "label": "cisco_ios", "extension": ".cfg"},
        {"name": "fw01", "type": "demo", "label": "fortigate", "extension": ".conf"},
        {"name": "unifi", "type": "demo", "label": "unifi", "extension": ".json"},
    ]}
    start = dt.datetime.now() - dt.timedelta(days=2)
    for n in range(3):
        state["run"] = n
        when = start + dt.timedelta(days=n)
        results, commit = core.run(cfg, demo_source=source, now=when)
        if n == 2:
            core.print_report(cfg, results, commit)
    print()
    print(core.git(str(folder), "show", "--color=always" if core.COLORS else "--color=never", "--format=%C(yellow)%h%Creset  %s",
                   "HEAD", "--", "sw-floor1.cfg").rstrip())
    print(core.color(f"\nDemo repository: {folder}  (try: git -C {folder} log --stat)", "2"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="netconfig-backup", description="Network device configurations, versioned in Git.")
    p.add_argument("--version", action="version", version=f"netconfig-backup {__version__}")
    p.add_argument("-c", "--config", default="devices.toml", help="configuration file (default: devices.toml)")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="back up every device and commit the changes (default)")
    r.add_argument("device", nargs="*", help="only these devices")
    sub.add_parser("test", help="check that every device answers, without writing anything")
    h = sub.add_parser("history", help="list the commits, for one device or all")
    h.add_argument("device", nargs="?")
    h.add_argument("-n", type=int, default=20)
    s = sub.add_parser("show", help="show what changed in a commit")
    s.add_argument("commit", nargs="?", default="HEAD")
    s.add_argument("device", nargs="?")
    d = sub.add_parser("demo", help="simulated network with a real Git history, nothing to configure")
    d.add_argument("folder", nargs="?")
    d.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    handlers = {"run": cmd_run, "test": cmd_test, "history": cmd_history, "show": cmd_show, "demo": cmd_demo, None: cmd_run}
    if args.cmd is None:
        args.device = []
    try:
        return handlers[args.cmd](args)
    except core.ConfigError as e:
        print(f"configuration: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
