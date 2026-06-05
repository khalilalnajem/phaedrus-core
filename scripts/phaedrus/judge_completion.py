"""
Phaedrus completion judge — decide which open_items in EXISTING cards actually
got DONE, so finished work stops cluttering daily triage.

Why this exists: the extractor (extract_card.py) now tags each open_item with a
status (done/open/uncertain) at extraction time, so NEW cards arrive judged. But
the 547 cards already on disk were extracted before that — they have no status,
so triage treats every one as still-open. This script back-fills status on those
existing cards by re-reading each card's SOURCE conversation and asking the LLM,
per item, "did Khalil actually finish this here?".

Discipline (Khalil's rule, baked into the prompt): NEVER guess "done". If the
conversation doesn't clearly show completion, the answer is "uncertain" — the
dashboard then ASKS rather than assuming. Cite-or-flag, not hallucinate.

Source recovery: a card's source is found by its session_id —
  web_captures/<id>.json  (the ~474 web cards: ChatGPT/Claude/Grok/Perplexity)
  ~/.claude/projects/-Users-khalilalnajem/<id>.jsonl  (Claude Code sessions)
If no source is found (~65 cards, mostly Grok naming quirks), it falls back to
judging from the card's own context+conclusion and leans hard to "uncertain".

Usage:
    python scripts/phaedrus/judge_completion.py --sample 20   # preview only, no writes to real cards
    python scripts/phaedrus/judge_completion.py --all         # write status into every card in place
    python scripts/phaedrus/judge_completion.py --ids a,b,c   # specific cards, in place
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from phaedrus_config import PHAEDRUS_HOME, _pick_llm_config
from extract_card import extract_messages, parse_card, CLAUDE_CODE_SESSIONS_DIR
from mem0.utils.factory import LlmFactory

CARDS_DIR = PHAEDRUS_HOME / "cards"
WEB_CAPTURES_DIR = PHAEDRUS_HOME / "web_captures"
PREVIEW_DIR = PHAEDRUS_HOME / "cards_judged_preview"

_CAP_STEMS = None


def _web_capture_stems():
    global _CAP_STEMS
    if _CAP_STEMS is None:
        _CAP_STEMS = {p.stem: p for p in WEB_CAPTURES_DIR.glob("*.json")}
    return _CAP_STEMS


def find_source_text(session_id: str):
    """Return (kind, text) for a card's source conversation, or (None, None).

    kind is 'web' / 'claude-code' / None. Tries a few session_id → file shapes
    because web capture filenames carry one or two source prefixes."""
    caps = _web_capture_stems()

    # web: strip the 'web_' the card adds, then try the stem and a de-duplicated
    # variant ('grok_grok_x' -> 'grok_x') since some captures single-prefix.
    cand = session_id[4:] if session_id.startswith("web_") else session_id
    tries = [cand]
    parts = cand.split("_", 1)
    if len(parts) == 2 and parts[1].startswith(parts[0] + "_"):
        tries.append(parts[1])          # grok_grok_x -> grok_x
    elif len(parts) == 2:
        tries.append("_".join([parts[0], parts[0], parts[1]]))  # grok_x -> grok_grok_x
    for t in tries:
        if t in caps:
            d = json.loads(caps[t].read_text())
            msgs = d.get("messages") or (d.get("raw") or {}).get("messages") or []
            lines = []
            for m in msgs:
                role = m.get("role") or m.get("author") or "?"
                content = m.get("content") or m.get("text") or ""
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if content:
                    lines.append(f"[{role}] {content}")
            if lines:
                return "web", "\n".join(lines)

    # claude code session jsonl
    jsonl = CLAUDE_CODE_SESSIONS_DIR / f"{session_id}.jsonl"
    if jsonl.exists():
        msgs = extract_messages(jsonl)
        if msgs:
            return "claude-code", "\n".join(f"[{m['role']}] {m['content']}" for m in msgs)

    return None, None


JUDGE_HEADER = """You are judging which of Khalil's stated to-dos from ONE conversation he actually FINISHED inside that same conversation. The goal: finished work should stop nagging him in his daily review; only genuinely-unfinished work should resurface.

For EACH numbered item below, decide its status using ONLY evidence in the material provided:
- "done": the conversation CLEARLY shows it was completed or resolved — he says he did it, the assistant confirms it shipped/works, the bug got fixed, the question got answered. Quote the proof in "reason".
- "open": still pending — raised but not resolved, deferred ("later", "next time"), or left hanging.
- "uncertain": the material does NOT make completion clear either way.

HARD RULES (this is the whole point — do not break them):
- NEVER mark "done" on a guess or assumption. "done" needs explicit evidence you can quote.
- When you are not sure, answer "uncertain". Khalil would far rather be ASKED than be told something is done when it isn't. Cite the evidence or admit uncertainty — no hallucinating completion.
- Default to "open" or "uncertain".

Return JSON ONLY, no prose, no fences:
{"judgments":[{"index":1,"status":"open|done|uncertain","reason":"<= 15 words, quote evidence if done"}, ...]}
One entry per item, SAME order and indices as listed.
"""


def build_judge_prompt(card: dict, source_kind, source_text, items):
    numbered = "\n".join(
        f'{i}. [{it.get("type","?")}] "{(it.get("text") or "").strip()}"'
        for i, it in enumerate(items, 1)
    )
    if source_text:
        material = f"CONVERSATION (source: {source_kind}):\n{source_text[:9000]}"
    else:
        # No source recovered — judge from the distilled card, lean to uncertain.
        material = (
            "NO full transcript is available — only this distilled card. You will "
            "usually NOT be able to confirm completion from a summary, so lean to "
            '"uncertain" unless the conclusion explicitly states an item was done.\n'
            f'TOPIC: {card.get("topic","")}\n'
            f'CONTEXT: {card.get("context","")}\n'
            f'CONCLUSION: {card.get("conclusion","")}'
        )
    return f"{JUDGE_HEADER}\n{material}\n\nITEMS:\n{numbered}\n"


def judge_card(llm, card: dict):
    """Return (judged_items, stats). Adds status/status_reason to a copy of
    each open_item. Never raises on a bad LLM reply — leaves items untouched."""
    items = list(card.get("open_items") or [])
    if not items:
        return items, {"items": 0, "done": 0, "open": 0, "uncertain": 0, "source": "none"}

    source_kind, source_text = find_source_text(card.get("_session_id") or card.get("_meta", {}).get("session_id", ""))
    prompt = build_judge_prompt(card, source_kind, source_text, items)

    raw = llm.generate_response(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    try:
        parsed = parse_card(raw)  # reuses the fence-stripping/repair parser
        judgments = {int(j["index"]): j for j in parsed.get("judgments", []) if "index" in j}
    except Exception:
        judgments = {}

    stats = {"items": len(items), "done": 0, "open": 0, "uncertain": 0,
             "source": source_kind or "card-only"}
    out = []
    for i, it in enumerate(items, 1):
        it = dict(it)
        j = judgments.get(i)
        status = (j or {}).get("status", "uncertain")
        if status not in ("done", "open", "uncertain"):
            status = "uncertain"
        it["status"] = status
        it["status_reason"] = (j or {}).get("reason", "no judgment returned — defaulting to uncertain")
        stats[status] += 1
        out.append(it)
    return out, stats


def load_cards(ids=None):
    cards = []
    paths = ([CARDS_DIR / f"{i}.json" for i in ids] if ids
             else sorted(CARDS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True))
    for p in paths:
        if not p.exists():
            print(f"  ⊘ no card {p.stem}"); continue
        try:
            c = json.loads(p.read_text())
            c["_session_id"] = p.stem
            c["_path"] = str(p)
            cards.append(c)
        except Exception as e:
            print(f"  ⊘ unreadable {p.stem}: {e}")
    return cards


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", type=int, help="judge N cards that HAVE open_items; preview only, no writes to real cards")
    g.add_argument("--all", action="store_true", help="judge every card, writing status in place")
    g.add_argument("--ids", help="comma-separated session ids to judge in place")
    args = ap.parse_args()

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"])

    if args.ids:
        cards = load_cards([s.strip() for s in args.ids.split(",") if s.strip()])
        preview = False
    elif args.sample:
        # only cards that actually have open_items are worth judging
        cards = [c for c in load_cards() if c.get("open_items")][: args.sample]
        preview = True
        PREVIEW_DIR.mkdir(exist_ok=True)
    else:
        cards = load_cards()
        preview = False

    print(f">>> Judging {len(cards)} card(s){' (PREVIEW — real cards untouched)' if preview else ' IN PLACE'}\n")

    totals = {"done": 0, "open": 0, "uncertain": 0, "items": 0}
    by_source = {}
    for n, card in enumerate(cards, 1):
        sid = card["_session_id"]
        try:
            judged, stats = judge_card(llm, card)
        except Exception as e:
            print(f"[{n}/{len(cards)}] {sid[:36]}  ✗ {e}")
            continue
        for k in ("done", "open", "uncertain", "items"):
            totals[k] += stats.get(k, 0)
        by_source[stats["source"]] = by_source.get(stats["source"], 0) + 1

        card["open_items"] = judged
        card.setdefault("_meta", {})["completion_judged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out_path = (PREVIEW_DIR if preview else CARDS_DIR) / f"{sid}.json"
        card_to_write = {k: v for k, v in card.items() if not k.startswith("_path")}
        out_path.write_text(json.dumps(card_to_write, indent=2, ensure_ascii=False))

        print(f"[{n}/{len(cards)}] {(card.get('topic') or sid)[:50]:50s}  "
              f"src={stats['source']:11s} done={stats['done']} open={stats['open']} unc={stats['uncertain']}")
        if preview:  # show the actual calls so they can be eyeballed
            for it in judged:
                mark = {"done": "✓", "open": "•", "uncertain": "?"}.get(it["status"], "•")
                print(f"        {mark} [{it['status']:9s}] {(it.get('text') or '')[:62]}")
                print(f"          ↳ {it.get('status_reason','')[:80]}")

    print("\n" + "=" * 70)
    print(f"TOTAL items judged: {totals['items']}  |  done {totals['done']}  ·  open {totals['open']}  ·  uncertain {totals['uncertain']}")
    print(f"source mix: {by_source}")
    if preview:
        print(f"\nPreview cards written to {PREVIEW_DIR} (your real cards are untouched).")
        print("If the done/open/uncertain calls look right, run:  python scripts/phaedrus/judge_completion.py --all")
    else:
        print("Status written into cards in place. Triage will now hide 'done' and ask on 'uncertain'.")


if __name__ == "__main__":
    main()
