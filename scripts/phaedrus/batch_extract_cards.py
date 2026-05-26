"""
Batch-extract Layer B conversation cards from Claude Code sessions.

For each session, calls Codex once to produce a structured card
(topic + context + conclusion + project_match + open_items + sparks + links)
and saves it to ~/.phaedrus/cards/<session-id>.json.

Resumes from existing cards on disk (idempotent).

Usage:
    python scripts/phaedrus/batch_extract_cards.py 5
    python scripts/phaedrus/batch_extract_cards.py 20
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from phaedrus_config import PHAEDRUS_HOME, _pick_llm_config, get_llm_mode
from extract_card import (
    extract_messages, format_for_prompt, EXTRACTION_PROMPT,
    parse_card, merge_regex_links, CLAUDE_CODE_SESSIONS_DIR
)
from mem0.utils.factory import LlmFactory

CARDS_DIR = PHAEDRUS_HOME / "cards"
CARDS_DIR.mkdir(exist_ok=True)
ERROR_LOG = PHAEDRUS_HOME / "card_extraction_errors.log"


def pick_sessions(limit: int, already: set) -> list:
    candidates = []
    for p in CLAUDE_CODE_SESSIONS_DIR.glob("*.jsonl"):
        if p.stem in already:
            continue
        try:
            lines = sum(1 for _ in p.open())
            if 50 <= lines <= 800:
                candidates.append((lines, p))
        except OSError:
            continue
    candidates.sort()
    n = len(candidates)
    if n == 0:
        return []
    middle = candidates[n // 4 : (n * 3) // 4] or candidates
    return [p for _, p in middle[:limit]]


def log_error(session_id: str, err: str) -> None:
    with ERROR_LOG.open("a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {session_id}: {err}\n")


def already_done() -> set:
    return {p.stem for p in CARDS_DIR.glob("*.json")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int)
    parser.add_argument("--max-messages", type=int, default=40)
    args = parser.parse_args()

    print(f">>> Phaedrus cards dir: {CARDS_DIR}")
    done = already_done()
    print(f">>> Already extracted: {len(done)} cards")

    targets = pick_sessions(args.count, done)
    print(f">>> Extracting cards from {len(targets)} new sessions")
    if not targets:
        print("nothing to do.")
        return

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"])

    summary = []
    for i, path in enumerate(targets, 1):
        session_id = path.stem
        print(f"\n[{i}/{len(targets)}] {session_id}")
        try:
            messages = extract_messages(path)
            if not messages:
                print("  ⊘ no user/assistant messages, skipping")
                continue

            formatted = format_for_prompt(messages, max_messages=args.max_messages)
            prompt = EXTRACTION_PROMPT + formatted

            t0 = time.time()
            raw = llm.generate_response(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            elapsed = time.time() - t0

            card = parse_card(raw)
            raw_text = "\n".join(m["content"] for m in messages)
            card = merge_regex_links(card, raw_text)
            card["_meta"] = {
                "session_id": session_id,
                "message_count": len(messages),
                "extraction_seconds": round(elapsed, 1),
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

            out_path = CARDS_DIR / f"{session_id}.json"
            out_path.write_text(json.dumps(card, indent=2, ensure_ascii=False))

            sparks = len(card.get("sparks", []))
            opens = len(card.get("open_items", []))
            links = len(card.get("links", []))
            project = card.get("project_match", {}).get("id", "?")
            confidence = card.get("project_match", {}).get("confidence", 0)
            topic = card.get("topic", "")[:60]
            print(f"  ✓ {elapsed:.0f}s | topic: {topic!r}")
            print(f"    project: {project} ({confidence}) | open: {opens} | sparks: {sparks} | links: {links}")
            summary.append({
                "session_id": session_id, "topic": topic, "project": project,
                "open_items": opens, "sparks": sparks, "links": links,
                "seconds": elapsed,
            })
        except KeyboardInterrupt:
            print("\n>>> interrupted; cards saved so far are valid.")
            break
        except Exception as e:
            print(f"  ✗ error: {e}")
            log_error(session_id, str(e))

    print()
    print("=" * 70)
    print(f"SUMMARY: {len(summary)} cards extracted")
    print("=" * 70)
    if summary:
        total_sparks = sum(s["sparks"] for s in summary)
        total_links = sum(s["links"] for s in summary)
        total_opens = sum(s["open_items"] for s in summary)
        avg_time = sum(s["seconds"] for s in summary) / len(summary)
        print(f"  Total sparks across sessions: {total_sparks}")
        print(f"  Total links captured:         {total_links}")
        print(f"  Total open items:             {total_opens}")
        print(f"  Avg extraction time/session:  {avg_time:.1f}s")
        print()
        print("Per-session:")
        for s in summary:
            print(f"  {s['session_id'][:8]}  proj={s['project']:20s}  opens={s['open_items']:2d}  sparks={s['sparks']:2d}  links={s['links']:2d}  ({s['seconds']:.0f}s)")
            print(f"           topic: {s['topic']}")


if __name__ == "__main__":
    main()
