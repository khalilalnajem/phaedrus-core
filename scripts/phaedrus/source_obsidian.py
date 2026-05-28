"""
Phaedrus Obsidian vault source adapter.

Walks the user's Obsidian vault (a folder of markdown files) and returns each
note as a session-equivalent. Notes are authored monologues, so we wrap each
as a single user message for the extractor.

Default vault: ~/Documents/Obsidian-Vault (auto-detected).
Override via env var: PHAEDRUS_OBSIDIAN_VAULT
"""
import os
import time
from pathlib import Path


def _find_vault() -> Path:
    """Look in the usual macOS locations for an Obsidian vault."""
    override = os.environ.get("PHAEDRUS_OBSIDIAN_VAULT")
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
    candidates = [
        Path.home() / "Documents" / "Obsidian-Vault",
        Path.home() / "Documents" / "Obsidian Vault",
        Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents",
        Path.home() / "Documents" / "ObsidianVault",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return Path()


VAULT_DIR = _find_vault()

# Skip Obsidian internals + common housekeeping folders
SKIP_DIRS = {".obsidian", ".trash", "templates", ".git", "_attachments", "Attachments"}


def find_notes(min_chars: int = 200, max_chars: int = 50_000) -> list:
    """Return Obsidian notes in the size window."""
    if not VAULT_DIR.exists():
        return []
    out = []
    for p in VAULT_DIR.rglob("*.md"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            size = p.stat().st_size
            if min_chars <= size <= max_chars:
                out.append(p)
        except OSError:
            continue
    return out


def extract_messages(note_path: Path) -> list:
    """Return the note's text as a single user-role message."""
    try:
        text = note_path.read_text(errors="replace")
    except Exception:
        return []
    text = text.strip()
    if not text:
        return []
    # Strip Obsidian frontmatter so the LLM doesn't get distracted by metadata
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            text = text[end + 3:].strip()
    if len(text) < 50:
        return []
    return [{"role": "user", "content": text}]


def session_id_from_path(note_path: Path) -> str:
    """Vault-relative path with slashes replaced by underscores, as a stable id."""
    rel = note_path.relative_to(VAULT_DIR) if VAULT_DIR in note_path.parents else note_path
    return "obsidian_" + str(rel).replace("/", "_").replace(" ", "-").replace(".md", "")


if __name__ == "__main__":
    print(f"Obsidian vault: {VAULT_DIR}")
    print(f"  exists: {VAULT_DIR.exists()}")
    notes = find_notes()
    print(f"  notes in size window: {len(notes)}")
    if notes:
        print(f"  sample: {notes[0].relative_to(VAULT_DIR)}")
        msgs = extract_messages(notes[0])
        if msgs:
            print(f"  first 200 chars: {msgs[0]['content'][:200]}")
