"""
Phaedrus web-captures source adapter.

Reads raw AI-chat captures written by the browser extension to
~/.phaedrus/web_captures/*.json. Unlike notes (authored monologues), these are
real back-and-forth conversations, so extract_messages returns the full
user/assistant message list.

Each capture file looks like:
{
  "received_at": "...", "source": "claude"|"chatgpt"|"gemini"|"grok"|"perplexity"|"deepseek",
  "id": "...", "url": "...", "title": "...",
  "messages": [{"role":"user","content":"..."}, {"role":"assistant","content":"..."}],
  "raw": { ... full extension payload ... }
}

Override the captures dir via env var: PHAEDRUS_WEB_CAPTURES_DIR
"""
import json
import os
from pathlib import Path

# Keep storage under ~/.phaedrus/ to match the rest of Phaedrus.
WEB_CAPTURES_DIR = Path(
    os.environ.get("PHAEDRUS_WEB_CAPTURES_DIR", "")
    or (Path.home() / ".phaedrus" / "web_captures")
).expanduser()

# Map the raw extension source/provider strings onto canonical platform names
# so card _meta.source is always one of claude/chatgpt/gemini/grok/perplexity/deepseek.
_PLATFORM_ALIASES = {
    "claude.ai": "claude",
    "claude": "claude",
    "chatgpt": "chatgpt",
    "chat.openai.com": "chatgpt",
    "openai": "chatgpt",
    "gemini": "gemini",
    "google gemini": "gemini",
    "grok": "grok",
    "x.ai": "grok",
    "perplexity": "perplexity",
    "perplexity.ai": "perplexity",
    "deepseek": "deepseek",
}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception:
        return {}


def find_captures() -> list:
    """Return capture file Paths in the web_captures dir (newest first)."""
    if not WEB_CAPTURES_DIR.exists():
        return []
    files = [p for p in WEB_CAPTURES_DIR.glob("*.json") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def canonical_source(raw_source: str) -> str:
    """Normalise an extension source/provider string to a canonical platform name."""
    s = (raw_source or "").strip().lower()
    if s in _PLATFORM_ALIASES:
        return _PLATFORM_ALIASES[s]
    # Fall back to a sanitised version of whatever was captured.
    return "".join(c for c in s if c.isalnum() or c in "-._") or "web"


def read_provenance(path: Path) -> dict:
    """Return {source, url, title} for a capture, for card _meta provenance."""
    data = _load(path)
    return {
        "source": canonical_source(data.get("source")),
        "url": data.get("url"),
        "title": data.get("title"),
    }


def extract_messages(path: Path) -> list:
    """Return the capture's messages as a clean role/content list (empties dropped)."""
    data = _load(path)
    raw_msgs = data.get("messages") or []
    out = []
    for m in raw_msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not content or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        out.append({"role": role or "user", "content": content})
    return out


def session_id_from_path(path: Path) -> str:
    """Stable id like 'web_<source>_<id>' derived from the capture."""
    data = _load(path)
    source = canonical_source(data.get("source"))
    cap_id = str(data.get("id") or "").strip()
    if not cap_id:
        # Filenames are written as <source>_<id>.json; recover the id from the stem,
        # else fall back to the whole stem.
        stem = path.stem
        cap_id = stem.split("_", 1)[1] if "_" in stem else stem
    safe_id = "".join(c for c in cap_id if c.isalnum() or c in "-._")[:60]
    return f"web_{source}_{safe_id}"


if __name__ == "__main__":
    print(f"Web captures dir: {WEB_CAPTURES_DIR}")
    print(f"  exists: {WEB_CAPTURES_DIR.exists()}")
    caps = find_captures()
    print(f"  captures: {len(caps)}")
    for p in caps:
        prov = read_provenance(p)
        msgs = extract_messages(p)
        sid = session_id_from_path(p)
        print(f"  {p.name}  source={prov['source']}  msgs={len(msgs)}  sid={sid}")
