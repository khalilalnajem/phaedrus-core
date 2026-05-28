"""
Phaedrus correction tracking + deterministic rules.

Two complementary learning mechanisms:

1. Stacked corrections (Approach A): every user correction recorded in
   ~/.phaedrus/corrections.jsonl. Last N injected into the extraction
   prompt as anti-examples.

2. Deterministic rules (Approach C): patterns the user has corrected
   repeatedly become post-processing rules in ~/.phaedrus/rules.jsonl.
   Applied after every extraction regardless of LLM behaviour.
"""
import json
import re
import time
from collections import Counter
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
CORRECTIONS_PATH = PHAEDRUS_HOME / "corrections.jsonl"
RULES_PATH = PHAEDRUS_HOME / "rules.jsonl"


# === Correction tracking ===

def record_correction(*, session_id: str, field: str, original: str, corrected: str, reason: str = "") -> None:
    """Persist a user correction."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "field": field,        # e.g. "open_items.type", "project_match.id", "ignore"
        "original": original,  # what Phaedrus extracted
        "corrected": corrected,  # what user said is right
        "reason": reason,      # optional plain-English reason
    }
    with CORRECTIONS_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_recent_corrections(n: int = 20) -> list:
    if not CORRECTIONS_PATH.exists():
        return []
    lines = CORRECTIONS_PATH.read_text().splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def format_corrections_for_prompt(n: int = 20) -> str:
    """Render recent corrections as anti-examples for the extraction prompt."""
    recs = load_recent_corrections(n)
    if not recs:
        return ""
    lines = ["RECENT CORRECTIONS (user has corrected these patterns before — DO NOT repeat the mistakes):"]
    for r in recs:
        field = r.get("field", "")
        orig = (r.get("original") or "")[:120]
        corr = (r.get("corrected") or "")[:120]
        reason = r.get("reason", "")
        if reason:
            lines.append(f"- In {field}: {orig!r} → should be {corr!r}. Reason: {reason}")
        else:
            lines.append(f"- In {field}: {orig!r} → should be {corr!r}.")
    return "\n".join(lines) + "\n\n"


# === Deterministic rules ===

def load_rules() -> list:
    if not RULES_PATH.exists():
        return []
    lines = RULES_PATH.read_text().splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def add_rule(*, name: str, applies_to: str, pattern: str, action: str, note: str = "") -> None:
    """Add a new deterministic rule. Run via repeat-correction detection or manual."""
    record = {
        "name": name,
        "applies_to": applies_to,  # e.g. "open_items", "sparks"
        "pattern": pattern,        # regex against item['text']
        "action": action,          # "demote_to_spark" | "drop" | "set_type:idea"
        "note": note,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with RULES_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def apply_rules(card: dict) -> dict:
    """Apply deterministic rules to a freshly extracted card. Returns modified card."""
    rules = load_rules()
    if not rules:
        return card
    for rule in rules:
        applies_to = rule.get("applies_to")
        pattern = rule.get("pattern", "")
        action = rule.get("action", "")
        if applies_to not in card or not pattern:
            continue
        items = card.get(applies_to, [])
        new_items = []
        for it in items:
            text = (it.get("text") if isinstance(it, dict) else "") or ""
            if not text:
                new_items.append(it)
                continue
            if re.search(pattern, text, flags=re.IGNORECASE):
                if action == "drop":
                    continue  # remove item entirely
                elif action == "demote_to_spark":
                    # move from open_items to sparks as type=idea
                    if applies_to == "open_items":
                        spark = {
                            "text": text,
                            "type": "idea",
                            "project": it.get("project", "none"),
                            "trigger_context": f"(demoted by rule: {rule.get('name', 'unnamed')})",
                        }
                        card.setdefault("sparks", []).append(spark)
                        continue  # don't keep in open_items
                elif action.startswith("set_type:"):
                    new_type = action.split(":", 1)[1]
                    it = dict(it)
                    it["type"] = new_type
            new_items.append(it)
        card[applies_to] = new_items
    return card


def detect_repeat_patterns(threshold: int = 3) -> list:
    """Walk corrections and suggest deterministic rules for patterns corrected 3+ times.
    Returns a list of (count, pattern_dict) tuples for review.
    """
    recs = load_recent_corrections(n=500)
    bucket = Counter()
    for r in recs:
        field = r.get("field", "")
        orig = (r.get("original") or "").strip().lower()
        corr = (r.get("corrected") or "").strip().lower()
        if not orig:
            continue
        # Bucket by leading bigram + field — naive but effective for "click X", "paste Y" patterns
        words = orig.split()
        if not words:
            continue
        head = " ".join(words[:2])
        bucket[(field, head, corr)] += 1
    suggestions = []
    for (field, head, corr), count in bucket.items():
        if count >= threshold:
            suggestions.append({
                "count": count,
                "field": field,
                "head_phrase": head,
                "target_correction": corr,
                "proposed_rule": {
                    "name": f"auto:{head[:30]}",
                    "applies_to": field.split(".")[0],
                    "pattern": re.escape(head),
                    "action": corr if corr in ("drop", "demote_to_spark") else "demote_to_spark",
                    "note": f"Auto-detected from {count} corrections",
                },
            })
    return suggestions


if __name__ == "__main__":
    # Dry run / status
    recs = load_recent_corrections(20)
    rules = load_rules()
    print(f"Corrections recorded: {len(recs)}")
    print(f"Rules in effect: {len(rules)}")
    if rules:
        print("\nRules:")
        for r in rules:
            print(f"  - {r.get('name')}: pattern={r.get('pattern')!r} action={r.get('action')}")
    suggestions = detect_repeat_patterns()
    if suggestions:
        print(f"\nSuggested rules from repeat patterns ({len(suggestions)}):")
        for s in suggestions:
            print(f"  {s['count']}x: {s['head_phrase']!r} → {s['proposed_rule']['action']}")
