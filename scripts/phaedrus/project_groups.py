"""
Phaedrus project grouping.

Real projects often span multiple folders/repos. Phaedrus = phaedrus + phaedrus-core
+ phaedrus-extension. Sanad = sanad + sanad-mvp + asksanad-landing + asksanad.app.

This module:
1. Auto-detects groupings from name prefixes and folder nesting.
2. Reads user overrides from ~/.phaedrus/project_groups.yml.
3. Collapses the registry into PARENT projects with child artefacts.

A "parent" project is the canonical name the user thinks in. Children are
the folders/repos that belong to it.

Run:
    python scripts/phaedrus/project_groups.py
"""
import json
import re
from collections import defaultdict
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
GROUPS_PATH = PHAEDRUS_HOME / "project_groups.json"
GROUPED_REGISTRY_PATH = PHAEDRUS_HOME / "projects_grouped.json"
REGISTRY_PATH = PHAEDRUS_HOME / "projects.json"


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"projects": {}}
    return json.loads(REGISTRY_PATH.read_text())


def load_user_overrides() -> dict:
    """Read user-defined groupings from ~/.phaedrus/project_groups.json."""
    if not GROUPS_PATH.exists():
        return {}
    try:
        return json.loads(GROUPS_PATH.read_text())
    except Exception:
        return {}


def auto_detect_parents(slugs: list) -> dict:
    """
    Detect parent-child relationships by name pattern:
    - phaedrus-core, phaedrus-extension → children of 'phaedrus'
    - asksanad-landing, sanad-mvp → children of 'sanad' (via 'sanad' substring)
    - Any X-Y where X is also a top-level slug → child of X

    Returns: {child_slug: parent_slug}
    """
    slug_set = set(slugs)
    mapping = {}

    for slug in slugs:
        if slug in mapping:
            continue
        # Case 1: hyphenated name like 'phaedrus-core' where 'phaedrus' is also a slug
        parts = slug.split("-")
        for i in range(len(parts) - 1, 0, -1):
            candidate = "-".join(parts[:i])
            if candidate in slug_set and candidate != slug:
                mapping[slug] = candidate
                break
        else:
            # Case 2: name contains another slug as substring (e.g. 'asksanad-landing' has 'sanad')
            for other in slug_set:
                if other == slug or len(other) < 4:
                    continue
                if other in slug and other != slug:
                    # only group if no closer prefix already matched
                    mapping[slug] = other
                    break

    return mapping


def build_grouped_view(registry: dict, overrides: dict, auto_map: dict) -> dict:
    """
    Merge auto + user-override mappings, then collapse the flat registry into
    a parent → children tree.

    User overrides win over auto-detection.
    """
    final_map = dict(auto_map)
    # Apply user overrides: {child: parent} or {"groups": [{parent: [children...]}]}
    if "groups" in overrides:
        for group in overrides["groups"]:
            parent = group.get("parent")
            for child in group.get("children", []):
                if parent and child:
                    final_map[child] = parent
    if "child_to_parent" in overrides:
        for child, parent in overrides["child_to_parent"].items():
            final_map[child] = parent
    if "exclude_from_parent" in overrides:
        for slug in overrides["exclude_from_parent"]:
            final_map.pop(slug, None)

    # Collapse into parent → [children] tree
    tree = defaultdict(lambda: {"info": None, "children": []})
    all_projects = registry.get("projects", {})

    for slug, info in all_projects.items():
        parent = final_map.get(slug)
        if parent and parent in all_projects:
            # Add as child of its parent
            tree[parent]["children"].append({"slug": slug, "info": info})
        elif slug in final_map.values() or slug not in final_map:
            # It's a parent (or standalone). Set the info node.
            if tree[slug]["info"] is None:
                tree[slug]["info"] = info
            elif tree[slug]["info"].get("status_evidence_days") and info.get("status_evidence_days"):
                # Keep the youngest signal
                if info["status_evidence_days"] < tree[slug]["info"]["status_evidence_days"]:
                    tree[slug]["info"] = info

    # Promote any parent that's missing its own info node (was inferred only as a parent)
    for parent_slug, node in tree.items():
        if node["info"] is None and node["children"]:
            # Use youngest child's status as the parent's
            youngest = min(
                (c["info"] for c in node["children"] if c["info"].get("status_evidence_days") is not None),
                key=lambda x: x.get("status_evidence_days", 999),
                default=node["children"][0]["info"],
            )
            node["info"] = {
                "id": parent_slug,
                "display_name": parent_slug.replace("-", " ").title(),
                "status": youngest.get("status", "active"),
                "status_evidence_days": youngest.get("status_evidence_days"),
                "inferred_parent": True,
            }

    return dict(tree)


def suggest_semantic_groupings(slugs: list, already_grouped: dict, rejected: list = None) -> list:
    """
    Use an LLM call to suggest semantic groupings the string rules can't see.
    Returns a list of {parent, children, reason} suggestions for user review.

    Example: nothing-application + job-applications (Nothing is a company being applied to).

    rejected: previously rejected groupings the user has corrected — fed to LLM as anti-examples.
    """
    if not slugs:
        return []
    rejected = rejected or []

    # Filter out slugs already grouped to keep the prompt tight
    candidates = [s for s in slugs if s not in already_grouped]
    if len(candidates) < 2:
        return []

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from phaedrus_config import _pick_llm_config
    from mem0.utils.factory import LlmFactory

    rejected_block = ""
    if rejected:
        rejected_block = "\n\nDO NOT SUGGEST these groupings — the user has rejected them before:\n"
        for r in rejected:
            kids = ", ".join(r.get("children", []))
            reason = r.get("reason", "")
            rejected_block += f"- {r.get('parent', '?')} ← {kids}. Reason rejected: {reason}\n"

    prompt = """You are looking at a list of folder/project names from a user's machine.
Some of them are semantically related and should be grouped under a common parent project,
EVEN IF their names don't share a prefix or substring.

Example: "nothing-application" and "job-applications" might both belong under "job-applications"
because Nothing is a company the user is applying to.

Project list:
""" + "\n".join(f"- {s}" for s in candidates) + rejected_block + """

Return JSON ONLY. No prose, no markdown fences. Schema:
{
  "groupings": [
    {
      "parent": "one of the slugs above (the meta-project)",
      "children": ["one or more slugs from the list above"],
      "reason": "one sentence explaining why these belong together semantically"
    }
  ]
}

Rules:
- Only suggest groupings where the relationship is genuinely meaningful, not just superficial.
- The parent MUST be one of the slugs in the list.
- Children must NOT include the parent.
- If you see no real semantic groupings beyond what string matching would catch, return {"groupings": []}.
"""
    llm_conf = _pick_llm_config()
    try:
        llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"])
        raw = llm.generate_response(
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"  ⚠ LLM suggestion failed: {e}")
        return []

    # Parse defensively
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1:
        return []
    try:
        data = json.loads(text[first:last + 1])
    except Exception:
        try:
            from json_repair import repair_json
            data = json.loads(repair_json(text[first:last + 1]))
        except Exception:
            return []
    return data.get("groupings", [])


def write_default_overrides_template():
    """Create a starter project_groups.json the user can edit."""
    if GROUPS_PATH.exists():
        return
    template = {
        "_comment": "Edit this file to override auto-detected project groupings. Phaedrus reads it on every registry refresh.",
        "_example_groups": [
            {"parent": "phaedrus", "children": ["phaedrus-core", "phaedrus-extension"]},
            {"parent": "sanad", "children": ["sanad-mvp", "asksanad-landing", "asksanad.app"]}
        ],
        "groups": [],
        "child_to_parent": {},
        "exclude_from_parent": []
    }
    GROUPS_PATH.write_text(json.dumps(template, indent=2))


def main():
    write_default_overrides_template()
    registry = load_registry()
    overrides = load_user_overrides()
    all_slugs = list(registry.get("projects", {}).keys())

    auto_map = auto_detect_parents(all_slugs)
    print(f">>> Auto-detected {len(auto_map)} child→parent relationships (string-based):")
    for child, parent in sorted(auto_map.items()):
        print(f"  {child:30s} → {parent}")

    # AI-suggested groupings for semantic relationships strings can't see.
    # Pass previously rejected suggestions so the LLM doesn't repeat its mistakes.
    rejected = overrides.get("rejected_suggestions", [])
    print(f"\n>>> Asking LLM for semantic grouping suggestions (one call, ~10s, {len(rejected)} prior rejections fed back)...")
    suggestions = suggest_semantic_groupings(all_slugs, auto_map, rejected=rejected)
    if suggestions:
        suggestions_path = PHAEDRUS_HOME / "suggested_groups.json"
        suggestions_path.write_text(json.dumps({"suggestions": suggestions}, indent=2, ensure_ascii=False))
        print(f">>> LLM suggested {len(suggestions)} semantic groupings (written to {suggestions_path}):")
        for s in suggestions:
            kids = ", ".join(s.get("children", []))
            print(f"  {s.get('parent', '?'):20s} ← {kids}")
            print(f"    reason: {s.get('reason', '')[:120]}")
        # Auto-apply suggestions to auto_map for this run (user can override later)
        for s in suggestions:
            parent = s.get("parent")
            for child in s.get("children", []):
                if parent and child and child != parent:
                    auto_map[child] = parent
    else:
        print(">>> No additional semantic groupings suggested.")

    tree = build_grouped_view(registry, overrides, auto_map)
    print(f"\n>>> Top-level projects after grouping: {len(tree)}")

    GROUPED_REGISTRY_PATH.write_text(json.dumps(tree, indent=2, ensure_ascii=False, default=str))
    print(f">>> Grouped registry written: {GROUPED_REGISTRY_PATH}")

    print("\n--- Active parent projects (with children) ---")
    actives = [(slug, node) for slug, node in tree.items()
               if node["info"] and node["info"].get("status") == "active"]
    actives.sort(key=lambda x: x[1]["info"].get("status_evidence_days") or 999)
    for slug, node in actives[:15]:
        d = node["info"].get("status_evidence_days")
        d_str = f"{d:.0f}d ago" if d is not None else "—"
        children = node.get("children", [])
        print(f"  {slug:25s} ({d_str})  +{len(children)} child folder(s)")
        for c in children:
            print(f"      └─ {c['slug']}")


def get_active_parent_projects() -> list:
    """Helper for the extractor: return parent project slugs in active status, sorted by recency."""
    if not GROUPED_REGISTRY_PATH.exists():
        return []
    try:
        tree = json.loads(GROUPED_REGISTRY_PATH.read_text())
    except Exception:
        return []
    actives = []
    for slug, node in tree.items():
        info = node.get("info") or {}
        if info.get("status") in ("active", "on-hold"):
            actives.append((slug, info.get("status_evidence_days") or 999))
    actives.sort(key=lambda x: x[1])
    return [slug for slug, _ in actives]


if __name__ == "__main__":
    main()
