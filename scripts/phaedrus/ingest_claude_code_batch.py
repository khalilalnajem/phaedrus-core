"""
Batch-ingest a slice of Claude Code sessions into Phaedrus.

Uses codex_cli (no API burn). Persists to ~/.phaedrus/. Resumes from
~/.phaedrus/ingested_sessions.txt so re-running skips already-done sessions.

Usage:
    python scripts/phaedrus/ingest_claude_code_batch.py 10           # ingest 10 new sessions
    python scripts/phaedrus/ingest_claude_code_batch.py 50 --max-msg 30  # 50 sessions, cap at 30 msgs each
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phaedrus_config import build_config, PHAEDRUS_HOME

from mem0 import Memory

CLAUDE_CODE_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-khalilalnajem"
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


def extract_messages(jsonl_path: Path):
    messages = []
    session_id = jsonl_path.stem
    first_ts = last_ts = None
    with jsonl_path.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue
            ts = obj.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            msg = obj.get("message", {})
            role = msg.get("role", t)
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content and isinstance(content, str):
                messages.append({"role": role, "content": content.strip()})

    return messages, {
        "source": "claude-code",
        "session_id": session_id,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "message_count": len(messages),
    }


def pick_sessions(limit: int, already: set) -> list:
    """Pick sessions by file size (medium-sized first), skipping already-ingested."""
    candidates = []
    for p in CLAUDE_CODE_SESSIONS_DIR.glob("*.jsonl"):
        if p.stem in already:
            continue
        try:
            size = p.stat().st_size
            candidates.append((size, p))
        except OSError:
            continue
    # Sort by size; pick from the middle of the distribution (substantial but not huge)
    candidates.sort()
    n = len(candidates)
    if n == 0:
        return []
    # Take from middle 50% of file-size distribution
    middle_start = n // 4
    middle_end = (n * 3) // 4
    middle = candidates[middle_start:middle_end] or candidates
    return [p for _, p in middle[:limit]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int, help="how many new sessions to ingest")
    parser.add_argument("--max-msg", type=int, default=20, help="cap on messages per session (cost control)")
    args = parser.parse_args()

    print(f">>> Phaedrus storage: {PHAEDRUS_HOME}")
    already = load_already_ingested()
    print(f">>> Already ingested: {len(already)} sessions")

    targets = pick_sessions(args.count, already)
    print(f">>> Picking {len(targets)} sessions to ingest")
    if not targets:
        print("nothing to do.")
        return

    m = Memory.from_config(build_config())
    print(">>> Memory ready.")
    print()

    success = 0
    for i, path in enumerate(targets, 1):
        session_id = path.stem
        print(f"[{i}/{len(targets)}] {session_id}")
        try:
            messages, metadata = extract_messages(path)
            if not messages:
                print(f"  ⊘ no user/assistant messages, skipping")
                mark_ingested(session_id)
                continue

            sample = messages[: args.max_msg]
            t0 = time.time()
            result = m.add(sample, user_id="khalil", metadata=metadata)
            elapsed = time.time() - t0
            added = len(result.get("results", []))
            print(f"  ✓ {added} facts in {elapsed:.1f}s ({metadata['message_count']} msgs in source)")
            mark_ingested(session_id)
            success += 1
        except KeyboardInterrupt:
            print("\n>>> interrupted; partial progress saved.")
            break
        except Exception as e:
            print(f"  ✗ error: {e}")
            log_error(session_id, str(e))

    print()
    print(f">>> Done. {success}/{len(targets)} sessions ingested cleanly.")
    print(f">>> Errors logged to: {ERROR_LOG}")
    print(f">>> Ingested log:     {INGESTED_LOG}")


if __name__ == "__main__":
    main()
