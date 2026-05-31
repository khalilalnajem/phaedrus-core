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
load_all_cards + get_triage_decisions + the same pending filter. Counting is
read-only — it never writes to ~/.phaedrus.
"""
import argparse
import hashlib
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


def item_key(session_id: str, kind: str, text: str) -> str:
    """Mirror of dashboard.item_key — must match exactly so decisions line up."""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{session_id}:{kind}:{h}"


def _is_pending(session_id: str, kind: str, text: str, decisions: dict, now_iso: str) -> bool:
    """A card item is pending unless it's been kept/ignored/done, or deferred
    with a later_until still in the future. Same rule as get_triage_candidates."""
    key = item_key(session_id, kind, text)
    d = decisions.get(key)
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

    conversations_pending = cards with at least one undecided item — the "N
    conversations to review" headline. items_pending = individual open_items +
    sparks still awaiting a decision. Unlike get_triage_candidates this applies
    NO limit / per-project cap: it's the true backlog, not the daily top slice.
    """
    cards = load_all_cards()
    decisions = get_triage_decisions()
    now_iso = datetime.now(timezone.utc).isoformat()

    conversations = 0
    items = 0
    for card in cards:
        sid = card["_session_id"]
        card_has_pending = False
        for it in card.get("open_items", []) or []:
            if _is_pending(sid, "open_item", it.get("text", ""), decisions, now_iso):
                items += 1
                card_has_pending = True
        for spark in card.get("sparks", []) or []:
            if _is_pending(sid, "spark", spark.get("text", ""), decisions, now_iso):
                items += 1
                card_has_pending = True
        if card_has_pending:
            conversations += 1
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
