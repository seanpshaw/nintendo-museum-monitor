#!/usr/bin/env python3
"""
Nintendo Museum (Kyoto/Uji) ticket watcher.

Polls the public calendar API behind https://museum-tickets.nintendo.com/en/calendar
and alerts you the moment one of your target dates flips to "on sale"
(usually a cancellation released first-come, first-served).

Alerts go to:
  - a macOS notification with sound (automatic on a Mac)
  - your phone via ntfy (free app, no account) if NTFY_TOPIC is set
  - the terminal

Usage:
  pip3 install curl_cffi
  python3 nintendo_museum_watch.py --test     # send a test alert, check once, exit
  python3 nintendo_museum_watch.py            # keep watching (Ctrl+C to stop)
  python3 nintendo_museum_watch.py --once     # single check (for cron/launchd)
"""

from __future__ import annotations  # lets this run on macOS's built-in Python 3.9

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ======================= SETTINGS — edit these =======================

# Dates you can visit (YYYY-MM-DD). Museum is closed Tuesdays (Dec 1 is a Tue).
TARGET_DATES = [
    "2026-12-02",
    "2026-12-03",
    "2026-12-04",
    "2026-12-17",
    # "2026-12-05",  # Sat — only works as an early slot before the 13:25 Kinosaki train
]

# Phone push: install the "ntfy" app, subscribe to this topic name.
# Pick something unguessable — anyone who knows the name can read/post to it.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # e.g. "sean-nintendo-museum-8f3k2"

CHECK_EVERY_SECONDS = 180   # base interval; a little random jitter is added
MIN_INTERVAL_SECONDS = 60   # don't hammer the site

# =====================================================================

BASE = "https://museum-tickets.nintendo.com"
API = f"{BASE}/en/api/calendar"
BUY_URL = f"{BASE}/en/calendar"
STATE_FILE = Path(__file__).with_name(".nintendo_museum_state.json")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BUY_URL,
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


class Blocked(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch_month(year: int, month: int) -> dict:
    """Return {date_str: info} for one month. info has sale_status / open_status."""
    from curl_cffi import requests  # imported here so --help works without it

    r = requests.get(
        API,
        params={"target_year": year, "target_month": month},
        headers=HEADERS,
        impersonate="chrome",
        timeout=30,
    )
    if r.status_code in (401, 403):
        raise Blocked(f"HTTP {r.status_code} — request blocked (VPN/cloud IP?)")
    r.raise_for_status()
    cal = r.json().get("data", {}).get("calendar")
    if cal is None:
        raise ValueError(f"Unexpected response shape: {r.text[:200]!r}")
    return cal


def status_label(info: dict | None) -> str:
    if not info:
        return "not listed"
    if info.get("open_status") != 1:
        return "closed"
    return {1: "AVAILABLE", 2: "sold out"}.get(info.get("sale_status"), f"status {info.get('sale_status')}")


def is_available(info: dict | None) -> bool:
    return bool(info) and info.get("open_status") == 1 and info.get("sale_status") == 1


def check(targets: list[str]) -> dict[str, dict | None]:
    months = sorted({(int(d[:4]), int(d[5:7])) for d in targets})
    results: dict[str, dict | None] = {}
    for y, m in months:
        cal = fetch_month(y, m)
        for d in targets:
            if (int(d[:4]), int(d[5:7])) == (y, m):
                results[d] = cal.get(d)
    return results


# ---------------------------- notifications ----------------------------

def notify_mac(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(body)} with title {json.dumps(title)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


def notify_phone(title: str, body: str) -> None:
    if not NTFY_TOPIC:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode(),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "rotating_light,video_game",
            "Click": BUY_URL,
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"ntfy push failed: {e}")


def alert(title: str, body: str) -> None:
    log(f"ALERT — {title}: {body}")
    notify_mac(title, body)
    notify_phone(title, body)
    if sys.platform != "darwin":
        print("\a", end="", flush=True)  # terminal bell elsewhere


# ------------------------------- state --------------------------------

def load_state() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()).get("available", []))
    except Exception:
        return set()


def save_state(available: set[str]) -> None:
    STATE_FILE.write_text(json.dumps({"available": sorted(available)}))


# -------------------------------- main --------------------------------

def run_once(targets: list[str]) -> None:
    results = check(targets)
    summary = ", ".join(f"{d[5:]} {status_label(i)}" for d, i in results.items())
    log(summary)

    now_available = {d for d, i in results.items() if is_available(i)}
    previously = load_state()
    new = sorted(now_available - previously)  # only alert on fresh openings
    if new:
        pretty = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d") for d in new)
        alert("Nintendo Museum tickets open!", f"{pretty} — grab them now")
    save_state(now_available)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--test", action="store_true", help="send a test alert, check once, exit")
    args = ap.parse_args()

    targets = sorted(d for d in TARGET_DATES if date.fromisoformat(d) > date.today())
    if not targets:
        log("All target dates have passed — nothing to watch.")
        return

    if args.test:
        alert("Nintendo Museum watcher test", "If you see this, alerts work.")
        run_once(targets)
        return
    if args.once:
        try:
            run_once(targets)
        except Blocked as e:
            log(f"{e}. This network can't reach the ticket site.")
            sys.exit(1)
        return

    interval = max(CHECK_EVERY_SECONDS, MIN_INTERVAL_SECONDS)
    log(f"Watching {', '.join(targets)} every ~{interval}s. Ctrl+C to stop.")
    failures = 0
    while True:
        targets = [d for d in targets if date.fromisoformat(d) > date.today()]
        if not targets:
            log("All target dates have passed — stopping.")
            return
        try:
            run_once(targets)
            failures = 0
        except Blocked as e:
            failures += 1
            log(f"{e}. Backing off.")
            if failures == 3:
                alert("Nintendo Museum watcher blocked", "Site is refusing requests — check VPN or network.")
        except Exception as e:
            failures += 1
            log(f"Check failed: {e}")
        backoff = min(failures, 5) * 120
        time.sleep(interval + backoff + random.uniform(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
