"""
Phaedrus capture-source ingester: Claude Code sessions.

Reads JSONL session files from ~/.claude/projects/-Users-khalilalnajem/<uuid>.jsonl,
extracts user + assistant messages, and feeds them into mem0 as a structured message list.
"""
import os
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/Users/khalilalnajem/Documents/phaedrus-core/.env", override=True)
sys.path.insert(0, str(Path(__file__).parent))
from phaedrus_config import build_config
from mem0 import Memory

CLAUDE_CODE_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-khalilalnajem"


def extract_messages(jsonl_path: Path):
    """Pull user + assistant messages from a session as a list of {role, content} dicts."""
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
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if content and isinstance(content, str):
                messages.append({"role": role, "content": content.strip()})

    metadata = {
        "source": "claude-code",
        "session_id": session_id,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "message_count": len(messages),
        "path": str(jsonl_path),
    }
    return messages, metadata


def pick_session(min_lines=200, max_lines=400):
    """Pick a session big enough to have substance but small enough to be cheap."""
    candidates = []
    for p in CLAUDE_CODE_SESSIONS_DIR.glob("*.jsonl"):
        try:
            lines = sum(1 for _ in p.open())
            candidates.append((lines, p))
        except Exception:
            continue
    candidates.sort()
    fits = [(n, p) for n, p in candidates if min_lines <= n <= max_lines]
    if fits:
        return fits[len(fits) // 2][1]
    return candidates[len(candidates) // 2][1]


def main():
    config = build_config()
    print(">>> Building Memory instance...")
    m = Memory.from_config(config)

    session_path = pick_session()
    print(f">>> Ingesting session: {session_path.name}")
    messages, metadata = extract_messages(session_path)
    print(f">>> {metadata['message_count']} messages; first 200 chars of first user msg: {next((msg['content'][:200] for msg in messages if msg['role']=='user'), '<none>')!r}")

    # Take first 10 messages (cap cost and noise)
    sample = messages[:10]
    print(f">>> Feeding first {len(sample)} messages to mem0.add() as a message list...")
    result = m.add(sample, user_id="khalil", metadata=metadata)
    print(">>> ADD result:")
    print(json.dumps(result, indent=2, default=str)[:2000])
    print()

    for q in ["what is Khalil working on?", "what is Phaedrus?", "Claude Code"]:
        print(f">>> SEARCH: {q!r}")
        r = m.search(q, filters={"user_id": "khalil"})
        for hit in r.get("results", [])[:3]:
            mem_str = hit['memory'][:150]
            print(f"    [{hit['score']:.3f}] {mem_str}")
        print()


if __name__ == "__main__":
    main()
