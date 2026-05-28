"""
Phaedrus Apple Notes source adapter.

Reads notes from the macOS Notes app via AppleScript (osascript), no third-party
binary required. Each note becomes a session-equivalent with a single user-role
message.

Requires: Automation permission for Terminal / the calling process to control Notes.
(Usually already granted if you've ever used 'osascript' for Notes before.)
"""
import re
import subprocess
import time
from pathlib import Path


def _osascript(script: str, timeout: int = 60) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()[:300]}")
    return result.stdout


def count_notes() -> int:
    try:
        out = _osascript('tell application "Notes" to return count of notes')
        return int(out.strip())
    except Exception:
        return 0


# AppleScript that returns notes as a delimited block. We use unusual delimiters
# unlikely to appear in real note content so we can split reliably.
_DELIM_FIELD = "<<PHAEDRUS_FIELD>>"
_DELIM_REC = "<<PHAEDRUS_REC>>"

_LIST_SCRIPT = f'''
tell application "Notes"
    set noteList to {{}}
    set noteIds to id of every note
    set noteNames to name of every note
    set noteMods to modification date of every note
    repeat with i from 1 to (count noteIds)
        set end of noteList to ((item i of noteIds) as text) & "{_DELIM_FIELD}" & ((item i of noteNames) as text) & "{_DELIM_FIELD}" & ((item i of noteMods) as text)
    end repeat
    set AppleScript's text item delimiters to "{_DELIM_REC}"
    set out to noteList as text
    set AppleScript's text item delimiters to ""
    return out
end tell
'''


def list_notes(min_age_days: float = 0, max_notes: int = None) -> list:
    """Return note metadata: [(note_id, title, modification_date_str), ...]."""
    try:
        raw = _osascript(_LIST_SCRIPT, timeout=120)
    except Exception as e:
        print(f"Apple Notes access failed: {e}")
        return []
    records = []
    for rec in raw.strip().split(_DELIM_REC):
        parts = rec.split(_DELIM_FIELD)
        if len(parts) == 3:
            records.append(tuple(p.strip() for p in parts))
    if max_notes:
        records = records[:max_notes]
    return records


def _read_note_body(note_id: str) -> str:
    """Pull a single note's plain text body."""
    # Escape any quotes in the id (rare but defensive)
    safe_id = note_id.replace('"', '\\"')
    script = f'''
tell application "Notes"
    set theNote to first note whose id is "{safe_id}"
    return plaintext of theNote
end tell
'''
    try:
        return _osascript(script, timeout=30).strip()
    except Exception:
        return ""


def extract_messages(note_id: str) -> list:
    """Return one user-role message containing the note's plain text body."""
    body = _read_note_body(note_id)
    body = body.strip()
    if not body or len(body) < 50:
        return []
    return [{"role": "user", "content": body}]


def session_id_from_id(note_id: str) -> str:
    """A stable session id for a Notes-app note (collapse the noisy CoreData URI)."""
    m = re.search(r"x-coredata://[^/]+/(.+?)(?:/|\?|$)", note_id)
    if m:
        compact = m.group(1).replace("/", "-").replace(" ", "")
        return f"applenotes_{compact}"
    return "applenotes_" + re.sub(r"[^a-zA-Z0-9]+", "-", note_id)[:60]


if __name__ == "__main__":
    print(f"Apple Notes count: {count_notes()}")
    records = list_notes(max_notes=5)
    print(f"Sample (first 5):")
    for note_id, title, mod in records:
        sid = session_id_from_id(note_id)
        print(f"  {title[:60]!r}  mod={mod[:20]}  sid={sid[:50]}")
        msgs = extract_messages(note_id)
        if msgs:
            print(f"    first 120 chars: {msgs[0]['content'][:120]!r}")
