"""
Phaedrus Codex CLI source adapter.

Reads conversation messages from ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
and returns them in the unified format Phaedrus expects (list of
{role, content} dicts).

Codex JSONL schema:
- Each line is a JSON object with `type` field
- type=session_meta: session-level metadata (skip for content)
- type=response_item: actual messages, with payload.role + payload.content[]
- type=event_msg, turn_context: control events (skip)

User messages often have system-injected blocks (AGENTS.md instructions,
environment_context, CMEM memory dumps). We filter these out so the
extractor sees the user's actual prompts.
"""
import json
from pathlib import Path

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Patterns that mark a content block as Codex-internal noise (not the user's actual writing)
INTERNAL_PREFIXES = (
    "# AGENTS.md",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<claude-mem-context>",
    "# $CMEM",
    "Legend: 🎯session",  # cmem context dump
    "<system-reminder>",
    "Codex CLI ",  # boot banner
)


def _is_internal_noise(text: str) -> bool:
    """True if this content block looks like Codex's auto-injected context, not real user input."""
    head = text.strip()[:200]
    return any(head.startswith(p) for p in INTERNAL_PREFIXES)


def extract_messages(jsonl_path: Path) -> list:
    """Pull user + assistant messages from a Codex session as {role, content}."""
    messages = []
    with jsonl_path.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload", {})
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue

            content = payload.get("content")
            text_parts = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btext = block.get("text", "")
                    if not btext or not isinstance(btext, str):
                        continue
                    if _is_internal_noise(btext):
                        continue
                    text_parts.append(btext.strip())
            elif isinstance(content, str):
                if not _is_internal_noise(content):
                    text_parts.append(content.strip())

            combined = "\n\n".join(p for p in text_parts if p)
            if combined:
                messages.append({"role": role, "content": combined})

    return messages


def session_id_from_path(jsonl_path: Path) -> str:
    """Use the rollout filename stem (or the embedded UUID) as the session id."""
    return jsonl_path.stem


def find_sessions(min_lines: int = 50, max_lines: int = 10000) -> list:
    """Return all Codex session JSONL paths in the size window."""
    out = []
    if not CODEX_SESSIONS_DIR.exists():
        return out
    for p in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            lines = sum(1 for _ in p.open())
            if min_lines <= lines <= max_lines:
                out.append(p)
        except OSError:
            continue
    return out


if __name__ == "__main__":
    # Quick stats on the user's Codex corpus
    paths = find_sessions(min_lines=20, max_lines=100000)
    print(f"Codex sessions found: {len(paths)}")
    if paths:
        sample = paths[0]
        msgs = extract_messages(sample)
        print(f"Sample: {sample.name}")
        print(f"Messages extracted (user + assistant, filtered): {len(msgs)}")
        if msgs:
            first_user = next((m for m in msgs if m["role"] == "user"), None)
            if first_user:
                print(f"First user message (first 200 chars): {first_user['content'][:200]}")
