"""
Generic batch ingester for note sources (Obsidian vault, Apple Notes).

Usage:
    python scripts/phaedrus/ingest_notes_batch.py obsidian 5
    python scripts/phaedrus/ingest_notes_batch.py apple-notes 5
    python scripts/phaedrus/ingest_notes_batch.py obsidian 20 --max-msg 1 --layer b

Notes are authored monologues, not back-and-forth conversations. The extractor
treats each as a single user message and pulls the same structured card
(topic, context, conclusion, memory_anchors, open_items, sparks, links, etc.)
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
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {session_id}: {err}\n")


def get_source(name: str):
    if name == "obsidian":
        from source_obsidian import find_notes, extract_messages, session_id_from_path, VAULT_DIR
        return {
            "name": "obsidian",
            "find": find_notes,
            "extract": extract_messages,
            "session_id": session_id_from_path,
            "label": str(VAULT_DIR) if VAULT_DIR else "(no vault found)",
        }
    elif name == "apple-notes":
        from source_apple_notes import list_notes, extract_messages, session_id_from_id
        def find():
            return [(rec[0], rec[1]) for rec in list_notes()]
        def extract(note_tuple):
            note_id = note_tuple[0] if isinstance(note_tuple, tuple) else note_tuple
            return extract_messages(note_id)
        def sid(note_tuple):
            note_id = note_tuple[0] if isinstance(note_tuple, tuple) else note_tuple
            return session_id_from_id(note_id)
        return {
            "name": "apple-notes",
            "find": find,
            "extract": extract,
            "session_id": sid,
            "label": "macOS Notes app via AppleScript",
        }
    raise ValueError(f"unknown source: {name}")


def pick_targets(source: dict, limit: int, already: set) -> list:
    candidates = source["find"]()
    # Filter out already-ingested
    fresh = []
    for c in candidates:
        sid = source["session_id"](c)
        if sid in already:
            continue
        fresh.append(c)
    return fresh[:limit]


def run_layer_a(m, messages, metadata, max_msg):
    sample = messages[:max_msg] if max_msg else messages
    t0 = time.time()
    result = m.add(sample, user_id="khalil", metadata=metadata)
    return len(result.get("results", [])), time.time() - t0


def run_layer_b(llm, messages, session_id, source_name):
    formatted = format_for_prompt(messages, max_messages=40)
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
        "source": source_name,
        "message_count": len(messages),
        "extraction_seconds": round(elapsed, 1),
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path = CARDS_DIR / f"{session_id}.json"
    out_path.write_text(json.dumps(card, indent=2, ensure_ascii=False))
    return card, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=["obsidian", "apple-notes"])
    parser.add_argument("count", type=int)
    parser.add_argument("--max-msg", type=int, default=1, help="cap on messages per session (notes are 1)")
    parser.add_argument("--layer", choices=["a", "b", "both"], default="both")
    args = parser.parse_args()

    source = get_source(args.source)
    print(f">>> Source: {source['name']} — {source['label']}")
    already = load_already_ingested()
    print(f">>> Already ingested: {len(already)} sessions")

    targets = pick_targets(source, args.count, already)
    print(f">>> {len(targets)} new notes to ingest")
    if not targets:
        print("nothing to do.")
        return

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    m = Memory.from_config(build_config()) if args.layer in ("a", "both") else None
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"]) if args.layer in ("b", "both") else None
    print()

    success = 0
    for i, target in enumerate(targets, 1):
        sid = source["session_id"](target)
        title = target if isinstance(target, Path) else (target[1] if isinstance(target, tuple) and len(target) > 1 else str(target))
        title_str = str(title)[:60]
        print(f"[{i}/{len(targets)}] {title_str}")
        try:
            messages = source["extract"](target)
            if not messages:
                print(f"  ⊘ empty / too short, skipping")
                mark_ingested(sid)
                continue
            metadata = {
                "source": source["name"],
                "session_id": sid,
                "message_count": len(messages),
            }
            if args.layer in ("a", "both"):
                n, t = run_layer_a(m, messages, metadata, args.max_msg)
                print(f"  Layer A: ✓ {n} facts in {t:.0f}s")
            if args.layer in ("b", "both"):
                card, t = run_layer_b(llm, messages, sid, source["name"])
                proj = card.get("project_match", {}).get("id", "?")
                opens = len(card.get("open_items", []))
                sparks = len(card.get("sparks", []))
                links = len(card.get("links", []))
                anchors = len(card.get("memory_anchors", []))
                print(f"  Layer B: ✓ card in {t:.0f}s | proj={proj} anchors={anchors} opens={opens} sparks={sparks} links={links}")
            mark_ingested(sid)
            success += 1
        except KeyboardInterrupt:
            print("\n>>> interrupted; partial progress saved.")
            break
        except Exception as e:
            print(f"  ✗ error: {e}")
            log_error(sid, str(e))

    print()
    print(f">>> Done. {success}/{len(targets)} {source['name']} notes ingested cleanly.")


if __name__ == "__main__":
    main()
