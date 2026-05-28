"""
Batch-ingest a slice of Codex CLI sessions into Phaedrus.

Mirrors ingest_claude_code_batch.py but reads from ~/.codex/sessions/
via source_codex.

Usage:
    python scripts/phaedrus/ingest_codex_batch.py 5
    python scripts/phaedrus/ingest_codex_batch.py 20 --max-msg 30
    python scripts/phaedrus/ingest_codex_batch.py 5 --layer b   # Layer B only
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from phaedrus_config import build_config, PHAEDRUS_HOME, _pick_llm_config
from extract_card import format_for_prompt, EXTRACTION_PROMPT, parse_card, merge_regex_links
from preferences import format_preferences_for_prompt
from corrections import format_corrections_for_prompt, apply_rules
from project_groups import get_active_parent_projects
from project_registry import get_active_projects
from project_descriptions import format_descriptions_for_prompt
from source_codex import extract_messages, find_sessions, session_id_from_path
from mem0.utils.factory import LlmFactory
from mem0 import Memory

CARDS_DIR = PHAEDRUS_HOME / "cards"
CARDS_DIR.mkdir(exist_ok=True)
INGESTED_LOG = PHAEDRUS_HOME / "ingested_sessions.txt"
ERROR_LOG = PHAEDRUS_HOME / "ingestion_errors.log"


def load_already_ingested() -> set:
    if INGESTED_LOG.exists():
        return set(INGESTED_LOG.read_text().splitlines())
    return set()


def mark_ingested(session_id: str) -> None:
    with INGESTED_LOG.open("a") as f:
        f.write(session_id + "\n")


def log_error(session_id: str, err: str) -> None:
    with ERROR_LOG.open("a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] codex/{session_id}: {err}\n")


def pick_targets(limit: int, already: set) -> list:
    candidates = []
    for p in find_sessions(min_lines=50, max_lines=20000):
        sid = session_id_from_path(p)
        if sid in already:
            continue
        candidates.append((p.stat().st_mtime, p))
    # Newest first — Codex sessions are short-lived task contexts, prefer recent
    candidates.sort(reverse=True)
    return [p for _, p in candidates[:limit]]


def run_layer_a(m: Memory, messages: list, metadata: dict, max_msg: int):
    sample = messages[:max_msg]
    t0 = time.time()
    result = m.add(sample, user_id="khalil", metadata=metadata)
    return len(result.get("results", [])), time.time() - t0


def run_layer_b(llm, messages: list, session_id: str, max_msg: int):
    formatted = format_for_prompt(messages, max_messages=max_msg)

    prefs_block = format_preferences_for_prompt()
    corrections_block = format_corrections_for_prompt(n=20)
    active = get_active_parent_projects() or get_active_projects()
    descriptions_block = format_descriptions_for_prompt(slugs=active) if active else ""

    prompt = prefs_block + corrections_block + descriptions_block + EXTRACTION_PROMPT + formatted

    t0 = time.time()
    raw = llm.generate_response(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    elapsed = time.time() - t0
    card = parse_card(raw)
    card = merge_regex_links(card, "\n".join(m["content"] for m in messages))
    card = apply_rules(card)
    card["_meta"] = {
        "session_id": session_id,
        "source": "codex",
        "message_count": len(messages),
        "extraction_seconds": round(elapsed, 1),
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path = CARDS_DIR / f"codex_{session_id}.json"
    out_path.write_text(json.dumps(card, indent=2, ensure_ascii=False))
    return card, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int, help="how many new Codex sessions to ingest")
    parser.add_argument("--max-msg", type=int, default=40, help="cap on messages per session")
    parser.add_argument("--layer", choices=["a", "b", "both"], default="both")
    args = parser.parse_args()

    print(f">>> Phaedrus storage: {PHAEDRUS_HOME}")
    print(f">>> Source: Codex CLI sessions at ~/.codex/sessions/")
    print(f">>> Layer: {args.layer}")
    already = load_already_ingested()
    print(f">>> Already ingested (across all sources): {len(already)} sessions")

    targets = pick_targets(args.count, already)
    print(f">>> Picking {len(targets)} new Codex sessions to ingest (most recent first)")
    if not targets:
        print("nothing to do.")
        return

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    m = Memory.from_config(build_config()) if args.layer in ("a", "both") else None
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"]) if args.layer in ("b", "both") else None
    print(">>> Ready.")
    print()

    success = 0
    for i, path in enumerate(targets, 1):
        session_id = session_id_from_path(path)
        print(f"[{i}/{len(targets)}] codex/{session_id}")
        try:
            messages = extract_messages(path)
            if not messages:
                print(f"  ⊘ no user/assistant messages, skipping")
                mark_ingested(session_id)
                continue
            print(f"  {len(messages)} messages")

            metadata = {
                "source": "codex",
                "session_id": session_id,
                "path": str(path),
                "message_count": len(messages),
            }

            if args.layer in ("a", "both"):
                n_facts, t = run_layer_a(m, messages, metadata, args.max_msg)
                print(f"  Layer A: ✓ {n_facts} atomic facts in {t:.0f}s")

            if args.layer in ("b", "both"):
                card, t = run_layer_b(llm, messages, session_id, max_msg=args.max_msg)
                proj = card.get("project_match", {}).get("id", "?")
                opens = len(card.get("open_items", []))
                sparks = len(card.get("sparks", []))
                links = len(card.get("links", []))
                anchors = len(card.get("memory_anchors", []))
                print(f"  Layer B: ✓ card in {t:.0f}s | proj={proj} anchors={anchors} opens={opens} sparks={sparks} links={links}")

            mark_ingested(session_id)
            success += 1
        except KeyboardInterrupt:
            print("\n>>> interrupted; partial progress saved.")
            break
        except Exception as e:
            print(f"  ✗ error: {e}")
            log_error(session_id, str(e))

    print()
    print(f">>> Done. {success}/{len(targets)} Codex sessions ingested cleanly.")


if __name__ == "__main__":
    main()
