"""
Phaedrus project registry.

Auto-discovers user's projects from multiple signal sources, assigns each
a status (Active / On-hold / Dormant / Finished / Archived), and writes the
result to ~/.phaedrus/projects.json so the extractor uses live data instead
of a hardcoded list.

User can override any project's status from the dashboard later.

Run:
    python scripts/phaedrus/project_registry.py
"""
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
REGISTRY_PATH = PHAEDRUS_HOME / "projects.json"
CARDS_DIR = PHAEDRUS_HOME / "cards"

DOCUMENTS = Path.home() / "Documents"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-khalilalnajem" / "memory"

# Folders we never treat as projects (system folders, large content libraries)
EXCLUDE = {
    ".DS_Store", "Library", "Downloads", "Desktop", "Pictures", "Music",
    "Movies", "Public", "Applications", "node_modules", ".Trash",
    "Obsidian Vault", "Obsidian-Vault",  # personal note vaults, not projects
}


def status_from_age(days_since_touch: float) -> str:
    if days_since_touch <= 30:
        return "active"
    if days_since_touch <= 90:
        return "on-hold"
    if days_since_touch <= 365:
        return "dormant"
    return "finished"


def scan_local_folders() -> dict:
    """Each top-level folder in ~/Documents/ is a candidate project."""
    found = {}
    if not DOCUMENTS.exists():
        return found
    now = time.time()
    for entry in DOCUMENTS.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDE or entry.name.startswith("."):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        days = (now - mtime) / 86400
        found[entry.name.lower()] = {
            "id": entry.name.lower().replace(" ", "-"),
            "display_name": entry.name,
            "path": str(entry),
            "last_touched_days_ago": round(days, 1),
            "signal_local_folder": True,
        }
    return found


def scan_memory_files() -> dict:
    """Read project_*.md files in ~/.claude/.../memory/ — they list user's tracked projects."""
    found = {}
    if not MEMORY_DIR.exists():
        return found
    for p in MEMORY_DIR.glob("project_*.md"):
        # extract slug from filename like project_sanad_mvp.md -> sanad-mvp
        slug = p.stem.replace("project_", "").replace("_", "-")
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        days = (time.time() - mtime) / 86400
        if slug not in found:
            found[slug] = {
                "id": slug,
                "display_name": slug.replace("-", " ").title(),
                "memory_file": str(p),
                "memory_last_touched_days_ago": round(days, 1),
                "signal_memory_file": True,
            }
    return found


def scan_card_mentions() -> dict:
    """Walk all extracted cards in ~/.phaedrus/cards/ and count project mentions."""
    found = Counter()
    if not CARDS_DIR.exists():
        return {}
    for p in CARDS_DIR.glob("*.json"):
        try:
            card = json.loads(p.read_text())
        except Exception:
            continue
        # Primary project from the card itself
        proj = card.get("project_match", {}).get("id", "")
        if proj and proj not in ("none", "new", ""):
            found[proj] += 2  # primary match is a strong signal
        # Secondary mentions in open_items, sparks, links
        for field in ("open_items", "sparks", "links", "people", "references", "numbers", "files"):
            for item in card.get(field, []) or []:
                if not isinstance(item, dict):
                    continue
                p2 = item.get("project", "")
                if p2 and p2 not in ("none", "new", ""):
                    found[p2] += 1
    out = {}
    for slug, count in found.items():
        out[slug] = {
            "id": slug,
            "display_name": slug.replace("-", " ").title(),
            "card_mentions": count,
            "signal_chat_mentions": True,
        }
    return out


def merge_sources(*sources: dict) -> dict:
    """Combine signal sources, keeping the richest entry per slug."""
    merged = {}
    for src in sources:
        for slug, info in src.items():
            if slug not in merged:
                merged[slug] = dict(info)
            else:
                merged[slug].update({k: v for k, v in info.items() if k not in merged[slug] or v})
    return merged


def assign_status(merged: dict) -> dict:
    """Compute status per project based on the strongest age signal we have."""
    for slug, info in merged.items():
        ages = []
        for key in ("last_touched_days_ago", "memory_last_touched_days_ago"):
            if key in info and isinstance(info[key], (int, float)):
                ages.append(info[key])
        if ages:
            youngest_signal = min(ages)
            info["status"] = status_from_age(youngest_signal)
            info["status_evidence_days"] = youngest_signal
        elif info.get("card_mentions", 0) >= 3:
            # No mtime but mentioned in cards a lot — treat as active
            info["status"] = "active"
            info["status_evidence_days"] = None
        else:
            info["status"] = "dormant"
            info["status_evidence_days"] = None
    return merged


def main():
    print(">>> Scanning project signal sources...")
    local = scan_local_folders()
    print(f"  ~/Documents/ folders: {len(local)}")
    mem = scan_memory_files()
    print(f"  memory project files: {len(mem)}")
    cards = scan_card_mentions()
    print(f"  chat-mention slugs in cards: {len(cards)}")

    merged = merge_sources(local, mem, cards)
    merged = assign_status(merged)
    print(f"\n>>> Total merged projects: {len(merged)}")

    # Group by status for the report
    by_status = Counter(info["status"] for info in merged.values())
    print(f"  active:   {by_status['active']}")
    print(f"  on-hold:  {by_status['on-hold']}")
    print(f"  dormant:  {by_status['dormant']}")
    print(f"  finished: {by_status['finished']}")

    # Write the registry
    registry = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects": merged,
    }
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    print(f"\n>>> Registry written: {REGISTRY_PATH}")

    print("\n--- Active projects ---")
    actives = sorted(
        [(slug, info) for slug, info in merged.items() if info["status"] == "active"],
        key=lambda x: x[1].get("status_evidence_days") or 999,
    )
    for slug, info in actives[:15]:
        d = info.get("status_evidence_days")
        d_str = f"{d:.0f}d ago" if d is not None else "no mtime"
        signals = ", ".join(k.replace("signal_", "") for k in info if k.startswith("signal_"))
        print(f"  {slug:30s}  {d_str:12s}  signals: {signals}")


def load_registry() -> dict:
    """Helper used by extract_card.py to read the registry for project_match."""
    if not REGISTRY_PATH.exists():
        return {"projects": {}}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except Exception:
        return {"projects": {}}


def get_active_projects() -> list:
    """Return a sorted list of active+on-hold project slugs for the extractor's project_match list."""
    reg = load_registry()
    out = []
    for slug, info in reg.get("projects", {}).items():
        status = info.get("status", "dormant")
        if status in ("active", "on-hold"):
            out.append((slug, info.get("status_evidence_days") or 999))
    out.sort(key=lambda x: x[1])
    return [slug for slug, _ in out]


if __name__ == "__main__":
    main()
