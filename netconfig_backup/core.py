"""Backups, Git history, notifications."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .drivers import DRIVERS, BackupError

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def _resolve_env(d: dict, where: str) -> dict:
    """foo_env = "VAR" -> foo = $VAR (secrets stay out of the configuration file)."""
    out = dict(d)
    for k, v in d.items():
        if k.endswith("_env") and isinstance(v, str) and v:
            if v not in os.environ:
                raise ConfigError(f"{where}: environment variable {v} is not set (for {k[:-4]})")
            out[k[:-4]] = os.environ[v]
    return out


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"configuration file not found: {p} (copy devices.example.toml)")
    with p.open("rb") as f:
        cfg = tomllib.load(f)
    base = p.parent
    backup = cfg.get("backup", {})
    backup.setdefault("repo", "backups")
    backup["repo"] = str((base / backup["repo"]).resolve())
    if backup.get("raw_dir"):
        backup["raw_dir"] = str((base / backup["raw_dir"]).resolve())
    defaults = cfg.get("defaults", {})
    devices, seen = [], set()
    for i, raw in enumerate(cfg.get("device", []), 1):
        dev = {**defaults, **raw}
        name = dev.get("name") or f"device{i}"
        if not SAFE_NAME.match(name):
            raise ConfigError(f"device {i}: name {name!r} may only contain letters, digits, '.', '_' and '-'")
        if name in seen:
            raise ConfigError(f"device {name}: duplicate name")
        seen.add(name)
        if dev.get("type") not in DRIVERS and dev.get("type") != "demo":
            raise ConfigError(f"device {name}: unknown type {dev.get('type')!r} (one of: {', '.join(DRIVERS)})")
        if not dev.get("host") and dev.get("type") != "demo":
            raise ConfigError(f"device {name}: host is missing")
        dev["name"] = name
        devices.append(dev)
    if not devices:
        raise ConfigError("no [[device]] in the configuration")
    return {"backup": backup, "notify": cfg.get("notify", {}), "devices": devices}


# ------------------------------------------------------------------------------------------
# Git
# ------------------------------------------------------------------------------------------
def git(repo: str, *args: str, check: bool = True, env: dict | None = None) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       env={**os.environ, **env} if env else None)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def ensure_repo(repo: str) -> None:
    Path(repo).mkdir(parents=True, exist_ok=True)
    if not (Path(repo) / ".git").exists():
        git(repo, "init", "-q", "-b", "main")
        (Path(repo) / "README.md").write_text(
            "# Network configuration backups\n\nManaged by [netconfig-backup](https://github.com/B2ZO0/netconfig-backup): "
            "one file per device, one commit per change.\n", encoding="utf-8")
    if not git(repo, "config", "user.email", check=False).strip():
        git(repo, "config", "user.name", "netconfig-backup")
        git(repo, "config", "user.email", "netconfig-backup@localhost")


# ------------------------------------------------------------------------------------------
# Backup run
# ------------------------------------------------------------------------------------------
@dataclass
class Result:
    name: str
    type: str
    status: str = "unchanged"          # unchanged | changed | new | failed
    error: str = ""
    added: int = 0
    removed: int = 0
    path: str = ""
    extra: dict = field(default_factory=dict)


def _fetch(dev: dict, demo_source=None) -> tuple[str, str]:
    """Returns (text for Git, raw text)."""
    if dev["type"] == "demo":
        raw = demo_source(dev)
        return raw, raw
    fetch, mask, _ = DRIVERS[dev["type"]]
    dev = _resolve_env(dev, f"device {dev['name']}")
    raw = fetch(dev)
    if not raw.strip():
        raise BackupError("empty configuration received")
    text = mask(raw) if (mask and dev.get("mask_secrets", True)) else raw
    return text, raw


def _ext(dev: dict) -> str:
    return dev.get("extension") or (DRIVERS[dev["type"]][2] if dev["type"] in DRIVERS else ".cfg")


def _keep_raw(backup: dict, dev: dict, raw: str, stamp: str) -> None:
    d = Path(backup["raw_dir"]) / dev["name"]
    d.mkdir(parents=True, exist_ok=True)
    def by_age(files):
        return sorted(files, key=lambda f: (f.stat().st_mtime_ns, f.name))

    files = by_age(d.glob("*" + _ext(dev)))
    if files and files[-1].read_text(encoding="utf-8", errors="replace") == raw:
        return
    f, i = d / f"{stamp}{_ext(dev)}", 1
    while f.exists():
        i += 1
        f = d / f"{stamp}-{i}{_ext(dev)}"
    f.write_text(raw, encoding="utf-8")
    os.chmod(f, 0o600)
    for old in by_age(d.glob("*" + _ext(dev)))[:-int(backup.get("raw_keep", 30))]:
        old.unlink()


def run(cfg: dict, demo_source=None, now: dt.datetime | None = None) -> tuple[list[Result], str]:
    backup = cfg["backup"]
    repo = backup["repo"]
    ensure_repo(repo)
    git_env = {"GIT_AUTHOR_DATE": now.isoformat(), "GIT_COMMITTER_DATE": now.isoformat()} if now else None
    now = now or dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    results: dict[str, Result] = {}

    def one(dev: dict) -> Result:
        res = Result(dev["name"], dev.get("label", dev["type"]))
        rel = f"{dev.get('folder', '').strip('/') + '/' if dev.get('folder') else ''}{dev['name']}{_ext(dev)}"
        res.path = rel
        target = Path(repo) / rel
        try:
            text, raw = _fetch(dev, demo_source)
        except (BackupError, ConfigError) as e:
            res.status, res.error = "failed", str(e)
            return res
        except Exception as e:  # noqa: BLE001 - unexpected, but one device must not stop the others
            res.status, res.error = "failed", f"{type(e).__name__}: {e}"
            return res
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(text, encoding="utf-8")
        if backup.get("raw_dir"):
            _keep_raw(backup, dev, raw, stamp)
        res.status = "changed" if existed else "new"
        return res

    workers = max(1, int(backup.get("workers", 4)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(one, cfg["devices"]):
            results[r.name] = r

    git(repo, "add", "-A")
    numstat = git(repo, "diff", "--cached", "--numstat")
    changed_paths = {}
    for line in numstat.splitlines():
        a, d, p = line.split("\t", 2)
        changed_paths[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    for r in results.values():
        if r.status in ("changed", "new"):
            if r.path in changed_paths:
                r.added, r.removed = changed_paths[r.path]
            elif r.status == "changed":
                r.status = "unchanged"
            else:
                r.status = "new"
    commit = ""
    if changed_paths:
        msg = summary(list(results.values()), now)
        body = "\n".join(f"- {r.name}: {r.status}" + (f" (+{r.added} -{r.removed})" if r.status == "changed" else "")
                         for r in results.values() if r.status in ("changed", "new"))
        git(repo, "commit", "-q", "-m", msg, "-m", body, env=git_env)
        commit = git(repo, "rev-parse", "--short", "HEAD").strip()
        if backup.get("git_push"):
            git(repo, "push", "-q", check=False)
    return [results[d["name"]] for d in cfg["devices"]], commit


def summary(results: list[Result], now: dt.datetime | None = None) -> str:
    c = {k: sum(1 for r in results if r.status == k) for k in ("changed", "new", "failed", "unchanged")}
    parts = [f"{c['changed']} changed"] if c["changed"] else []
    if c["new"]:
        parts.append(f"{c['new']} new")
    if c["failed"]:
        parts.append(f"{c['failed']} failed")
    if not parts:
        parts.append("no change")
    return f"Backup {(now or dt.datetime.now()).strftime('%Y-%m-%d %H:%M')}: " + ", ".join(parts)


# ------------------------------------------------------------------------------------------
# Notifications
# ------------------------------------------------------------------------------------------
def notify(cfg: dict, results: list[Result], commit: str) -> None:
    n = _resolve_env(cfg.get("notify", {}), "[notify]")
    when = n.get("on", "change")
    changed = [r for r in results if r.status in ("changed", "new")]
    failed = [r for r in results if r.status == "failed"]
    if when == "never" or (when == "change" and not (changed or failed)) or (when == "failure" and not failed):
        return
    lines = [summary(results)]
    lines += [f"✎ {r.name} (+{r.added} −{r.removed})" if r.status == "changed" else f"✚ {r.name}" for r in changed]
    lines += [f"✘ {r.name}: {r.error}" for r in failed]
    if commit:
        lines.append(f"commit {commit}")
    text = "\n".join(lines)
    try:
        if n.get("telegram_bot_token") and n.get("telegram_chat_id"):
            data = urllib.parse.urlencode({"chat_id": n["telegram_chat_id"], "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage", data=data, timeout=15)
        if n.get("webhook_url"):
            req = urllib.request.Request(n["webhook_url"], data=json.dumps({"content": text, "text": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001 - a failed notification must not fail the backup
        print(f"notification failed: {e}", file=sys.stderr)


# ------------------------------------------------------------------------------------------
# Console output
# ------------------------------------------------------------------------------------------
COLORS = (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))) and os.environ.get("NO_COLOR") is None


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLORS else text


def print_report(cfg: dict, results: list[Result], commit: str) -> None:
    w = max(len(r.name) for r in results)
    t = max(len(r.type) for r in results)
    print(color(f"netconfig-backup {__version__}", "1;35") + color(f" · {len(results)} device(s) · {cfg['backup']['repo']}", "2"))
    for r in results:
        icon, col, label = {"unchanged": ("✔", "32", "unchanged"), "changed": ("✎", "33", "changed"),
                            "new": ("✚", "36", "new"), "failed": ("✘", "31", "failed")}[r.status]
        extra = f"+{r.added} −{r.removed}" if r.status == "changed" else (r.error if r.status == "failed" else "")
        print(f"  {color(icon, col)} {r.name.ljust(w)}  {color(r.type.ljust(t), '2')}  {color(label.ljust(9), col)} {extra}")
    print(color(f"Commit {commit}: {summary(results)}", "1") if commit else color("No change: nothing to commit.", "2"))
