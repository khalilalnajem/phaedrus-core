"""
Phaedrus unified project config.

ONE editable file at ~/.phaedrus/projects.yml replaces three scattered files:
- project_groups.json (overrides, rejection memory)
- project_descriptions.md (one-liners)
- projects_grouped.json (the computed view, still written here for tools that read it)

Auto-seeded from the existing scattered files on first run. After that, the
user edits projects.yml as the single source of truth. The other scripts
(project_groups.py, extract_card.py) read from this file via the helpers below.

Run:
    python scripts/phaedrus/projects_config.py                    # show current state
    python scripts/phaedrus/projects_config.py --edit             # open in editor
    python scripts/phaedrus/projects_config.py --migrate          # rebuild from scattered files
"""
import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
CONFIG_PATH = PHAEDRUS_HOME / "projects.yml"

# Legacy files we migrate from (and continue to write through for backwards-compat)
LEGACY_DESCRIPTIONS = PHAEDRUS_HOME / "project_descriptions.md"
LEGACY_GROUPS = PHAEDRUS_HOME / "project_groups.json"
LEGACY_GROUPED_REGISTRY = PHAEDRUS_HOME / "projects_grouped.json"
LEGACY_REGISTRY = PHAEDRUS_HOME / "projects.json"

HEADER = """# Phaedrus Projects — single source of truth.
#
# Edit this file freely. Saved changes take effect on the next refresh / extraction.
#
# Each top-level key is a project slug. Fields:
#   description: one-line plain-English description (used by the LLM for routing decisions)
#   status:      active | on-hold | dormant | finished | archived
#                (auto-set from folder mtime; you can override)
#   children:    list of child slugs that belong under this parent project
#   excluded_from_parent: list of slugs that should NEVER be auto-grouped under this project
#   rejected_suggestions: history of LLM grouping suggestions you rejected (helps the AI learn)
#
# Lines starting with # are comments and are ignored.

"""


# === Migration from scattered files ===

def _load_legacy_descriptions() -> dict:
    if not LEGACY_DESCRIPTIONS.exists():
        return {}
    out = {}
    for line in LEGACY_DESCRIPTIONS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        m = re.match(r"^([a-zA-Z0-9._\-/]+)\s*(?:—|--|-)\s*(.+)$", line)
        if m:
            slug, desc = m.group(1).strip().lower(), m.group(2).strip()
            if slug and desc and len(desc) > 5 and slug != "slug-name":
                out[slug] = desc
    return out


def _load_legacy_groups() -> dict:
    if not LEGACY_GROUPS.exists():
        return {}
    try:
        return json.loads(LEGACY_GROUPS.read_text())
    except Exception:
        return {}


def _load_registry() -> dict:
    if not LEGACY_REGISTRY.exists():
        return {"projects": {}}
    try:
        return json.loads(LEGACY_REGISTRY.read_text())
    except Exception:
        return {"projects": {}}


def migrate_from_scattered_files() -> dict:
    """Build the unified config from all legacy sources."""
    descriptions = _load_legacy_descriptions()
    groups = _load_legacy_groups()
    registry = _load_registry()

    # Build child→parent map from groups overrides
    child_to_parent = {}
    if "groups" in groups:
        for g in groups["groups"]:
            parent = g.get("parent")
            for child in g.get("children", []):
                if parent and child:
                    child_to_parent[child] = parent
    if "child_to_parent" in groups:
        child_to_parent.update(groups["child_to_parent"])

    excluded = set(groups.get("exclude_from_parent", []))
    rejected = groups.get("rejected_suggestions", [])

    # Build unified project entries
    config = {}
    all_slugs = set(registry.get("projects", {}).keys()) | set(descriptions.keys())

    for slug in sorted(all_slugs):
        reg_info = registry.get("projects", {}).get(slug, {})
        entry = {
            "description": descriptions.get(slug, ""),
            "status": reg_info.get("status", "dormant"),
            "children": [c for c, p in child_to_parent.items() if p == slug],
        }
        if slug in excluded:
            entry["excluded_from_parent"] = True
        # Attach rejection history that involves this project as a proposed parent
        slug_rejections = [r for r in rejected if r.get("parent") == slug]
        if slug_rejections:
            entry["rejected_suggestions"] = [
                {"children": r.get("children", []), "reason": r.get("reason", "")}
                for r in slug_rejections
            ]
        config[slug] = entry

    return config


# === Load / save ===

def ensure_config() -> Path:
    """Create projects.yml from scattered files if it doesn't exist."""
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    config = migrate_from_scattered_files()
    write_config(config)
    return CONFIG_PATH


def load_config() -> dict:
    ensure_config()
    with CONFIG_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    return data


def write_config(config: dict) -> None:
    text = HEADER + yaml.safe_dump(
        config,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    )
    CONFIG_PATH.write_text(text)


# === Helpers for other modules ===

def get_descriptions() -> dict:
    """Returns {slug: description}."""
    config = load_config()
    return {slug: info.get("description", "") for slug, info in config.items() if info.get("description")}


def get_parent_children_map() -> dict:
    """Returns {child_slug: parent_slug}."""
    config = load_config()
    out = {}
    for parent, info in config.items():
        for child in info.get("children") or []:
            out[child] = parent
    return out


def get_excluded() -> set:
    config = load_config()
    return {slug for slug, info in config.items() if info.get("excluded_from_parent")}


def get_rejected_suggestions() -> list:
    config = load_config()
    out = []
    for parent, info in config.items():
        for r in info.get("rejected_suggestions", []) or []:
            out.append({"parent": parent, "children": r.get("children", []), "reason": r.get("reason", "")})
    return out


def add_rejected_suggestion(*, parent: str, children: list, reason: str) -> None:
    config = load_config()
    if parent not in config:
        config[parent] = {"description": "", "status": "dormant"}
    config[parent].setdefault("rejected_suggestions", []).append({
        "children": children,
        "reason": reason,
    })
    # Also exclude these from parent
    for child in children:
        if child in config:
            config[child]["excluded_from_parent"] = True
    write_config(config)


# === CLI ===

def show_summary():
    config = load_config()
    active = []
    on_hold = []
    dormant = []
    archived = []
    for slug, info in config.items():
        status = info.get("status", "dormant")
        if status == "active":
            active.append((slug, info))
        elif status == "on-hold":
            on_hold.append((slug, info))
        elif status == "archived":
            archived.append((slug, info))
        else:
            dormant.append((slug, info))

    print(f"\n=== Phaedrus Project Config (~/.phaedrus/projects.yml) ===")
    print(f"  Total projects: {len(config)}")
    print(f"  Active: {len(active)}  |  On-hold: {len(on_hold)}  |  Dormant: {len(dormant)}  |  Archived: {len(archived)}")

    def render_group(name, items):
        if not items:
            return
        print(f"\n--- {name} ({len(items)}) ---")
        for slug, info in sorted(items):
            desc = (info.get("description") or "(no description)")[:80]
            children = info.get("children") or []
            kids = f" +{len(children)} child" if children else ""
            print(f"  {slug:30s}{kids:12s}  {desc}")
            for c in children[:5]:
                print(f"    └─ {c}")

    render_group("ACTIVE", active)
    render_group("ON-HOLD", on_hold)
    render_group("ARCHIVED", archived)
    print(f"\n  (Dormant projects hidden. Total dormant: {len(dormant)})")
    print(f"\n  Edit:  open -a TextEdit {CONFIG_PATH}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edit", action="store_true", help="open the config in $EDITOR")
    parser.add_argument("--migrate", action="store_true", help="rebuild from scattered legacy files")
    args = parser.parse_args()

    if args.migrate:
        config = migrate_from_scattered_files()
        write_config(config)
        print(f"Rebuilt from scattered files: {CONFIG_PATH}")
        return

    ensure_config()

    if args.edit:
        editor = os.environ.get("EDITOR", "open -a TextEdit")
        subprocess.run(f"{editor} {CONFIG_PATH}", shell=True)
        return

    show_summary()


if __name__ == "__main__":
    main()
