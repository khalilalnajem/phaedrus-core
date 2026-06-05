"""
Phaedrus daily nudge.

Phaedrus is a *daily* review tool, but nothing reminds you to open it — so
captured conversations pile up untriaged (the Collector's Fallacy). This script
counts today's pending triage items and fires one native macOS notification:

    Phaedrus · 14 conversations to review

Click it (when terminal-notifier is installed) to open the dashboard at
http://localhost:5050. Schedule it once a day with install_notify.sh.

Run:
    python scripts/phaedrus/daily_notify.py            # count + fire one notification
    python scripts/phaedrus/daily_notify.py --dry-run  # count + print, fire nothing

This file is intentionally standalone. It does NOT import dashboard.py (which
would boot Flask). It replicates dashboard.py's read-only counting logic:
load_all_cards + get_triage_decisions + the same per-conversation pending
filter (decision key = session_id). Counting is read-only — never writes.
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
CARDS_DIR = PHAEDRUS_HOME / "cards"
TRIAGE_LOG = PHAEDRUS_HOME / "triage_log.jsonl"

DASHBOARD_URL = "http://localhost:5050"
NOTIFY_TITLE = "Phaedrus"


# === Read-only counting (mirrors dashboard.py, no Flask, no writes) ===

def load_all_cards():
    """Mirror of dashboard.load_all_cards (read-only)."""
    cards = []
    if not CARDS_DIR.exists():
        return cards
    for p in sorted(CARDS_DIR.glob("*.json")):
        try:
            card = json.loads(p.read_text())
            card["_session_id"] = p.stem
            cards.append(card)
        except Exception:
            continue
    return cards


def get_triage_decisions():
    """Mirror of dashboard.get_triage_decisions (read-only)."""
    decided = {}
    if TRIAGE_LOG.exists():
        for line in TRIAGE_LOG.read_text().splitlines():
            try:
                d = json.loads(line)
                decided[d["key"]] = d
            except Exception:
                continue
    return decided


def _card_pending(session_id: str, decisions: dict, now_iso: str) -> bool:
    """A conversation is pending unless it's been kept/ignored/done, or deferred
    with a later_until still in the future. Triage is per-conversation now, so
    the decision key is the session_id — must match dashboard.get_triage_cards."""
    d = decisions.get(session_id)
    if not d:
        return True
    decision = d.get("decision")
    if decision in ("keep", "ignore", "done"):
        return False
    if decision == "later":
        until = d.get("later_until")
        if until and now_iso < until:
            return False
    return True


def count_pending():
    """Return (conversations_pending, items_pending).

    conversations_pending = undecided conversations — the "N conversations to
    review" headline, matching the dashboard's per-card triage. items_pending =
    open_items + sparks inside those pending conversations, shown in the body for
    context. NO limit / per-project cap: this is the true backlog, not a slice.
    """
    cards = load_all_cards()
    decisions = get_triage_decisions()
    now_iso = datetime.now(timezone.utc).isoformat()

    conversations = 0
    items = 0
    for card in cards:
        triage_items = (card.get("open_items") or []) + (card.get("sparks") or [])
        if not triage_items:
            continue
        if _card_pending(card["_session_id"], decisions, now_iso):
            conversations += 1
            items += len(triage_items)
    return conversations, items


# === Notification ===

def build_message(conversations: int, items: int):
    """Return (title, message) for the notification.

    Headline counts conversations (matches the dashboard's per-card view); the
    item count rides along in the body for context.
    """
    conv_word = "conversation" if conversations == 1 else "conversations"
    title = f"{NOTIFY_TITLE} · {conversations} {conv_word} to review"
    item_word = "item" if items == 1 else "items"
    message = f"{items} {item_word} waiting · open your daily review"
    return title, message


def _osascript_escape(s: str) -> str:
    """Escape for an AppleScript double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def fire_notification(title: str, message: str) -> bool:
    """Fire a macOS notification. Prefers terminal-notifier (supports click-to-open
    via -open); falls back to plain osascript (no reliable click action).

    Returns True on success.
    """
    tn = shutil.which("terminal-notifier")
    if tn:
        try:
            subprocess.run(
                [
                    tn,
                    "-title", NOTIFY_TITLE,
                    "-subtitle", title.replace(f"{NOTIFY_TITLE} · ", ""),
                    "-message", message,
                    "-open", DASHBOARD_URL,
                    "-group", "com.phaedrus.notify",
                ],
                check=True,
            )
            return True
        except Exception as e:
            print(f"terminal-notifier failed ({e}); falling back to osascript", file=sys.stderr)

    # Plain osascript fallback. macOS notifications fired this way cannot carry
    # a reliable click action, so the URL goes into the message text instead.
    body = f"{message} — {DASHBOARD_URL}"
    script = (
        f'display notification "{_osascript_escape(body)}" '
        f'with title "{_osascript_escape(title)}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True)
        return True
    except Exception as e:
        print(f"osascript failed: {e}", file=sys.stderr)
        return False


# === Entry point ===

def main(argv=None):
    parser = argparse.ArgumentParser(description="Fire a daily Phaedrus triage nudge.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the count and message instead of firing a notification.")
    args = parser.parse_args(argv)

    conversations, items = count_pending()
    title, message = build_message(conversations, items)

    if conversations == 0:
        # Nothing to nudge about. Stay quiet on a real run; report on dry-run.
        if args.dry_run:
            print("conversations pending: 0")
            print("items pending:", items)
            print("(nothing to triage — no notification would fire)")
        return 0

    if args.dry_run:
        print(f"conversations pending: {conversations}")
        print(f"items pending: {items}")
        print(f"title:   {title}")
        print(f"message: {message}")
        print(f"opens:   {DASHBOARD_URL}")
        backend = "terminal-notifier (click-to-open)" if shutil.which("terminal-notifier") else "osascript (plain, no click)"
        print(f"backend: {backend}")
        return 0

    ok = fire_notification(title, message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
