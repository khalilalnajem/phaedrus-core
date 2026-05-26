"""
Phaedrus Action Layer: daily triage renderer.

Walks all cards in ~/.phaedrus/cards/. Pulls every open_item and spark.
Scores by recency × emotional weight × no-prior-decision. Picks top 7.
Renders each as a 2-3 line triage card for the user.

This is the action layer. The thing that makes Phaedrus useful daily.

Usage:
    python scripts/phaedrus/daily_triage.py
    python scripts/phaedrus/daily_triage.py --top 7
    python scripts/phaedrus/daily_triage.py --top 15  # see more than the daily cap
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phaedrus_config import PHAEDRUS_HOME

CARDS_DIR = PHAEDRUS_HOME / "cards"
TRIAGE_LOG = PHAEDRUS_HOME / "triage_log.jsonl"   # tracks what's been surfaced + decided


def load_cards():
    cards = []
    for p in CARDS_DIR.glob("*.json"):
        try:
            card = json.loads(p.read_text())
            card["_path"] = str(p)
            card["_session_id"] = p.stem
            cards.append(card)
        except Exception:
            continue
    return cards


def load_decisions():
    """Items the user has already triaged (Keep/Ignore/Later) won't resurface."""
    decided = {}  # key: "session:item_index_or_text_hash" → "keep"/"ignore"/"later_until"
    if TRIAGE_LOG.exists():
        for line in TRIAGE_LOG.read_text().splitlines():
            try:
                d = json.loads(line)
                decided[d["key"]] = d
            except Exception:
                continue
    return decided


def item_key(session_id: str, kind: str, text: str) -> str:
    """Stable key per item so decisions persist."""
    import hashlib
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{session_id}:{kind}:{h}"


def score_item(item: dict, card: dict, decisions: dict) -> float:
    """Higher score = more urgent to surface."""
    key = item_key(card["_session_id"], item["_kind"], item["text"])

    # Skip if already decided (unless Later expired)
    if key in decisions:
        d = decisions[key]
        if d.get("decision") == "ignore" or d.get("decision") == "done":
            return -1
        if d.get("decision") == "later":
            until = d.get("later_until")
            if until and datetime.now(timezone.utc).isoformat() < until:
                return -1

    score = 0.0

    # Base by type
    type_weights = {
        "commitment": 1.0,
        "worry":      0.7,
        "question":   0.5,
        "idea":       0.4,
        "observation": 0.3,
        "tangent":    0.3,
        "side-question": 0.5,
    }
    score += type_weights.get(item.get("type", ""), 0.3)

    # Due date urgency
    due = item.get("due")
    if due:
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_until = (due_dt - now).days
            if days_until < 0:
                score += 2.0  # overdue
            elif days_until == 0:
                score += 1.5  # due today
            elif days_until <= 3:
                score += 1.0
            elif days_until <= 7:
                score += 0.5
        except Exception:
            pass

    # Age boost: older items that have never been surfaced get a small bump
    meta = card.get("_meta", {})
    extracted_at = meta.get("extracted_at")
    if extracted_at:
        try:
            extracted_dt = datetime.fromisoformat(extracted_at.replace("Z", "+00:00"))
            days_old = (datetime.now(timezone.utc) - extracted_dt).days
            if days_old > 7:
                score += 0.3 * min(days_old / 30, 1.0)  # caps at +0.3
        except Exception:
            pass

    # Project status boost: items on active projects rank higher
    project_match = card.get("project_match", {})
    try:
        conf = float(project_match.get("confidence", 0))
        if conf > 0.7:
            score += 0.2
    except (TypeError, ValueError):
        pass

    return score


def render_triage_line(item: dict, card: dict) -> str:
    """Produce the 2-3 line triage shape the user sees in their channel."""
    topic = card.get("topic", "untitled conversation").strip()
    text = item.get("text", "").strip()
    item_type = item.get("type", "")
    project = item.get("project") or card.get("project_match", {}).get("id", "")

    project_tag = f" [{project}]" if project and project not in ("none", "new") else ""

    if item_type == "commitment":
        due = item.get("due")
        due_str = f" by {due}" if due else ""
        return f"📌 {topic}{project_tag}\n   You said you'd: {text}{due_str}\n   Done? Still pending? Killing it? (k / i / l)"

    if item_type == "worry":
        return f"⚠ {topic}{project_tag}\n   You raised: {text}\n   Where is this now? Resolved? Still open? (k / i / l)"

    if item_type == "question":
        return f"❓ {topic}{project_tag}\n   Left open: {text}\n   Pick this up? Or shelve? (k / i / l)"

    if item_type == "idea":
        return f"💡 {topic}{project_tag}\n   Idea you had: {text}\n   Act on it now? Or shelve? (k / i / l)"

    if item_type in ("observation", "tangent", "side-question"):
        return f"📝 {topic}{project_tag}\n   Spark: {text}\n   Worth coming back to? (k / i / l)"

    return f"  {topic}{project_tag}\n   {text}\n   (k / i / l)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=7, help="how many cards to surface")
    parser.add_argument("--show-scores", action="store_true", help="print scores next to each item")
    args = parser.parse_args()

    cards = load_cards()
    print(f">>> Cards on disk: {len(cards)}")
    if not cards:
        print("Nothing to triage. Run the ingester first.")
        return

    decisions = load_decisions()
    print(f">>> Prior decisions: {len(decisions)}")

    # Flatten open_items + sparks across all cards
    candidates = []
    for card in cards:
        for item in card.get("open_items", []):
            item["_kind"] = "open_item"
            candidates.append((item, card))
        for spark in card.get("sparks", []):
            spark["_kind"] = "spark"
            candidates.append((spark, card))

    print(f">>> Total candidates: {len(candidates)} (open_items + sparks across all cards)")

    # Score and rank
    scored = []
    for item, card in candidates:
        s = score_item(item, card, decisions)
        if s >= 0:
            scored.append((s, item, card))
    scored.sort(key=lambda x: -x[0])

    top = scored[: args.top]
    print(f">>> Surfacing top {len(top)} of {len(scored)} eligible items")
    print()
    print("=" * 70)
    print(f"PHAEDRUS DAILY TRIAGE — {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 70)
    print()

    for i, (score, item, card) in enumerate(top, 1):
        prefix = f"[{i}/{len(top)}]"
        if args.show_scores:
            prefix += f" score={score:.2f}"
        print(prefix)
        print(render_triage_line(item, card))
        print()

    print("=" * 70)
    print(f"Reply with the number + decision: e.g. '3k' (keep #3), '5i' (ignore #5), '1l' (later #1)")
    print(f"Decisions logged at: {TRIAGE_LOG}")


if __name__ == "__main__":
    main()
