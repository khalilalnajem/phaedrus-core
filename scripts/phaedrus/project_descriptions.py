"""
Phaedrus project descriptions.

Plain-markdown file (~/.phaedrus/project_descriptions.md) where the user writes
ONE LINE per project saying what it actually is. This prevents the LLM from
guessing wrong on similarly-named projects.

Auto-seeded from existing card context on first run. User edits afterwards.
Loaded by extract_card.py (project_match) and project_groups.py (semantic
suggestions) so descriptions inform every prompt.
"""
import json
import re
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
DESCRIPTIONS_PATH = PHAEDRUS_HOME / "project_descriptions.md"
CARDS_DIR = PHAEDRUS_HOME / "cards"

HEADER = """# Phaedrus Project Descriptions

One line per project. Format:

    slug-name — short description of what this project actually is

This file is read on every extraction and every grouping refresh. The LLM
uses these descriptions to route conversations correctly and avoid grouping
projects that just SOUND related but aren't.

The list below was auto-seeded from your existing cards. Edit, delete, or
add as you see fit.

"""


def seed_from_cards() -> dict:
    """Walk cards and build a starter description per project from topic + context."""
    out = {}
    if not CARDS_DIR.exists():
        return out
    by_slug = {}
    for p in CARDS_DIR.glob("*.json"):
        try:
            card = json.loads(p.read_text())
        except Exception:
            continue
        proj_id = card.get("project_match", {}).get("id", "")
        if not proj_id or proj_id in ("none", "new"):
            continue
        topic = (card.get("topic") or "").strip()
        if not topic:
            continue
        by_slug.setdefault(proj_id, []).append(topic[:120])
    # Take the most representative topic per slug (the longest one, naive)
    for slug, topics in by_slug.items():
        out[slug] = max(topics, key=len)
    return out


def ensure_default_descriptions() -> Path:
    """Create the descriptions file with auto-seeded content if it doesn't exist."""
    if DESCRIPTIONS_PATH.exists():
        return DESCRIPTIONS_PATH
    seeded = seed_from_cards()
    lines = [HEADER]
    for slug, desc in sorted(seeded.items()):
        lines.append(f"{slug} — {desc}")
    lines.append("")
    DESCRIPTIONS_PATH.write_text("\n".join(lines))
    return DESCRIPTIONS_PATH


def load_descriptions() -> dict:
    """Parse the markdown file into {slug: description}."""
    ensure_default_descriptions()
    out = {}
    for line in DESCRIPTIONS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # Accept both em-dash and double-hyphen as separators
        m = re.match(r"^([a-zA-Z0-9._\-/]+)\s*(?:—|--|-)\s*(.+)$", line)
        if m:
            slug, desc = m.group(1).strip().lower(), m.group(2).strip()
            if slug and desc and len(desc) > 5:
                out[slug] = desc
    return out


def format_descriptions_for_prompt(slugs: list = None) -> str:
    """Return descriptions as a prompt block. If slugs given, filter to those."""
    descs = load_descriptions()
    if slugs is not None:
        descs = {s: d for s, d in descs.items() if s in slugs}
    if not descs:
        return ""
    lines = ["PROJECT DESCRIPTIONS (read these BEFORE making decisions about projects):"]
    for slug, desc in sorted(descs.items()):
        lines.append(f"- {slug}: {desc}")
    lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    p = ensure_default_descriptions()
    print(f"Descriptions file: {p}")
    descs = load_descriptions()
    print(f"Parsed {len(descs)} descriptions:")
    for slug, desc in sorted(descs.items())[:10]:
        print(f"  {slug:30s} — {desc[:80]}")
    if len(descs) > 10:
        print(f"  ... and {len(descs) - 10} more")
    print(f"\nEdit the file with:")
    print(f"  open -a TextEdit {p}")
