#!/usr/bin/env python3
"""Minimal push channel for the tracker: Telegram first, ntfy as fallback.

Configuration is environment-only; nothing secret lives in the repo:

  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   primary channel
  NTFY_URL (e.g. https://ntfy.sh/<topic>) fallback channel
  NOTIFY_QUEUE                           queue file (default: notify_queue.jsonl here)
  NOTIFY_TZ_OFFSET                       local UTC offset in hours (default 8, MYT)

Messages are only delivered 16:00-23:59 local time. Anything pushed outside that
window is appended to the queue, never dropped, and delivered by `--flush`
(run from cron at 16:05) or by the next in-window push.

push(msg) returns the channel used ("telegram" / "ntfy"), "queued", or None.
It never raises: a notifier crash must not take the caller down with it.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

WINDOW_START_HOUR = 16   # inclusive, local
WINDOW_END_HOUR = 24     # exclusive, local
TZ = dt.timezone(dt.timedelta(hours=float(os.environ.get("NOTIFY_TZ_OFFSET", "8"))))
QUEUE_PATH = Path(os.environ.get("NOTIFY_QUEUE",
                                 Path(__file__).resolve().parent / "notify_queue.jsonl"))


def in_window(now=None):
    h = (now or dt.datetime.now(TZ)).hour
    return WINDOW_START_HOUR <= h < WINDOW_END_HOUR


def _telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
        return json.loads(r.read()).get("ok") is True


def _ntfy(text):
    url = os.environ.get("NTFY_URL")
    if not url:
        return False
    urllib.request.urlopen(urllib.request.Request(url, data=text.encode()), timeout=20)
    return True


def _send(text):
    for name, fn in (("telegram", _telegram), ("ntfy", _ntfy)):
        try:
            if fn(text):
                return name
        except Exception as e:  # noqa: BLE001 -- try the next channel
            print(f"notify: {name} failed: {e}", file=sys.stderr)
    return None


def _enqueue(msg):
    try:
        with open(QUEUE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                                 "msg": msg}) + "\n")
        return True
    except OSError as e:
        print(f"notify: could not queue: {e}", file=sys.stderr)
        return False


def flush():
    """Deliver queued messages oldest first; keep any that fail. -> number sent."""
    if not QUEUE_PATH.exists():
        return 0
    pending = [json.loads(ln) for ln in QUEUE_PATH.read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    kept, sent = [], 0
    for rec in pending:
        if _send(f"(queued {rec['ts'][:16].replace('T', ' ')})\n{rec['msg']}"):
            sent += 1
        else:
            kept.append(rec)
    QUEUE_PATH.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
    return sent


def push(msg):
    try:
        if not in_window():
            return "queued" if _enqueue(msg) else None
        flush()
        return _send(msg)
    except Exception as e:  # noqa: BLE001
        print(f"notify: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if "--flush" in sys.argv:
        print(f"sent {flush()} queued message(s)")
    else:
        print(f"window {WINDOW_START_HOUR:02d}:00-23:59 local; in window now: {in_window()}")
