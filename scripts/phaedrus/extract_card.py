"""
Phaedrus Layer B: conversation card extractor.

Reads one Claude Code session JSONL, asks the LLM (Codex via OAuth) to
produce ONE structured card per conversation:

  topic, context, conclusion, project_match, open_items[], sparks[]

This is the daily-triage substrate. Different output shape than mem0's
atomic-fact extraction (Layer A). Both layers run on the same source.

Usage:
    python scripts/phaedrus/extract_card.py <session-id-or-path>
    python scripts/phaedrus/extract_card.py --pick     # auto-pick a substantive session
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from phaedrus_config import _pick_llm_config, get_llm_mode
from preferences import format_preferences_for_prompt
from corrections import format_corrections_for_prompt, apply_rules
from project_registry import get_active_projects
from project_groups import get_active_parent_projects
from project_descriptions import format_descriptions_for_prompt
from mem0.utils.factory import LlmFactory

CLAUDE_CODE_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-khalilalnajem"

# Known projects for project_match. Will be auto-detected from ~/Documents later.
KNOWN_PROJECTS = [
    "sanad-mvp", "phaedrus", "field-engine", "khalil-company",
    "raneen", "marker", "magiclayers", "khalilalnajem.com",
    "asksanad.app", "job-applications", "ai-vault",
]


EXTRACTION_PROMPT = """You are extracting structured intelligence from a conversation between Khalil and an AI assistant.

Return ONE JSON object with this exact shape. No prose. No markdown fences. JSON only.

{
  "topic": "6-10 words. What is this conversation about?",
  "context": "3-4 sentences (longer than you might think). What was being weighed, what trade-offs, what background was needed to make sense of the discussion?",
  "conclusion": "2-3 sentences. What got decided, what got committed, what was left open?",
  "memory_anchors": [
    "2-4 SPECIFIC vivid details that make this conversation instantly recognisable later. NOT a summary — specific quotable details. Examples: a person's name + their role ('Shaymaa said deadline is Tuesday'), a specific number ('$30K stipend cut'), a vivid phrase the user used ('I just realised vLLM might be cheaper'), or a unique moment ('the conversation pivoted when we found the Pactify pricing page'). Each anchor is 5-15 words, concrete and quoteable. These are what jog the user's memory."
  ],
  "project_match": {
    "id": "one of: """ + ", ".join(KNOWN_PROJECTS) + """ or 'none' or 'new'",
    "confidence": "0.0 to 1.0"
  },
  "open_items": [
    {
      "text": "Specific actionable. Direct quote or near-quote.",
      "type": "commitment | worry | idea | question",
      "project": "matched project id or 'none'",
      "due": "ISO date if explicit, else null",
      "status": "done | open | uncertain — see ITEM STATUS rules. Default open/uncertain, NEVER guess done.",
      "status_reason": "1 short sentence: the evidence FROM THE CONVERSATION for this status (quote if you can).",
      "where_to_use": "1 sentence: WHEN and WHERE this should be surfaced for action. Examples: 'Before next MOHE check-in this week — Shaymaa is waiting on this', 'When planning sanad-mvp infrastructure for v2', 'First thing tomorrow morning while still fresh'. Concrete, contextual, actionable."
    }
  ],
  "sparks": [
    {
      "text": "Quote the line. Capture EVERY actionable thought, every 'btw', every 'what if', every tangent. Scan all messages, not just start and end.",
      "trigger_context": "1 sentence: what surrounded this in the conversation",
      "project": "matched project id or 'none'",
      "type": "idea | observation | tangent | side-question",
      "where_to_use": "1 sentence: WHEN and WHERE this spark would be useful to revisit. Examples: 'When evaluating self-hosting options for sanad-mvp', 'Next time someone asks about the Plato Phaedrus reference', 'If pricing-pages decision comes back up in v2 planning'. Specific situation/moment."
    }
  ],
  "links": [
    {
      "url": "any URL mentioned in the conversation (https://..., github.com/..., x.com/..., twitter.com/...)",
      "context": "1 sentence: what was happening when this link came up, what it was for",
      "type": "github-repo | github-profile | tweet | x-profile | article | tool | reference | other",
      "project": "matched project id or 'none'"
    }
  ],
  "people": [
    {
      "name": "Person's name as written (preserve spelling)",
      "role_or_context": "1 sentence: who they are or why they came up (advisor, collaborator, customer, friend, etc.)",
      "project": "matched project id or 'none'"
    }
  ],
  "references": [
    {
      "name": "Book / podcast / tool / product / paper name (NOT a URL)",
      "type": "book | podcast | tool | product | paper | course | other",
      "context": "1 sentence: how it came up in conversation",
      "project": "matched project id or 'none'"
    }
  ],
  "numbers": [
    {
      "value": "The number / amount / metric as stated",
      "what": "What it measures (cost, percentage, deadline, headcount, etc.)",
      "context": "1 sentence: what was happening when this number came up",
      "project": "matched project id or 'none'"
    }
  ],
  "files": [
    {
      "path": "File path as written (e.g. ~/Documents/sanad/sanad-mvp/docs/ROADMAP.md)",
      "context": "1 sentence: what this file was for or being discussed about",
      "project": "matched project id or 'none'"
    }
  ]
}

CRITICAL INSTRUCTIONS:
- Scan EVERY message from start to finish. The conversation may have sparks in the first message, the middle, or the end. All positions matter equally.
- Capture sparks even if the user did NOT follow up on them. They are valuable precisely because they were not pursued.
- If the conversation has any substance, MINIMUM 3 sparks. Empty sparks[] ONLY if the conversation is truly trivial (greeting, one-word reply, etc.).
- Use direct quotes from the conversation for sparks and open_items text where possible.

MEMORY ANCHORS (very important):
- 2-4 specific vivid details that will make the user INSTANTLY remember this conversation 3 months from now.
- NOT a summary. Specific quoteable details. Names + roles. Specific numbers. Vivid phrases. Unique moments.
- Bad anchor: 'discussed Sanad migration' (too vague)
- Good anchor: 'Khalil mentioned the $30 cost cap and Shaymaa's Tuesday deadline'
- Good anchor: 'Pivoted when we found the Pactify pricing page'

WHERE_TO_USE per item (very important):
- Don't just say WHAT to do, say WHEN/WHERE it should resurface.
- Bad: 'follow up on this' (no context)
- Good: 'Before next MOHE check-in this week — Shaymaa is waiting on this'
- Good: 'When planning sanad-mvp infrastructure for v2'
- Good: 'Next time the topic of Pactify pricing comes back up'
- For project_match: match against the user's known projects above. If the conversation does not relate to any, return id='none' with low confidence.
- For links: capture EVERY external URL mentioned anywhere in the conversation (https://..., http://..., github.com/..., x.com/..., twitter.com/...). Include the URL exactly as written, plus 1 sentence of surrounding context so Khalil can remember why it came up. Do NOT include file paths, localhost URLs, or internal tool URLs.

COMMITMENT DEFINITION (very important — DO NOT over-classify):

A COMMITMENT is something Khalil says HE will do, ON HIS OWN TIME, with intent to act later. Format pattern: "I will X" / "I'll do Y" / "I need to Z by Tuesday" / "I'm going to ship X this week".

A commitment must satisfy ALL of these:
- Khalil himself is the actor (NOT the assistant)
- The action is scheduled for AFTER the conversation ends (NOT during it)
- It carries real intent, not idle musing

DO NOT classify as commitments:
- Instructions to the AI assistant ("click here", "paste this", "open that tab", "run this command")
- Steps Khalil performs WHILE the conversation is happening ("I just clicked it", "I'm pasting now", "screenshot taken")
- Things the user explicitly said they already did during the chat itself
- Recommendations the assistant made TO Khalil ("you should run X") unless Khalil explicitly accepted with intent
- Hypothetical or exploratory statements ("we could try X" without "I'll")

If something is more spark than commitment (an idea Khalil mentioned without committing to act), put it in sparks[] with type=idea, NOT in open_items[] with type=commitment.

ITEM STATUS (critical — this decides what resurfaces in daily triage; DO NOT GUESS):
For every open_item, judge whether Khalil ACTUALLY completed or resolved it, based ONLY on what THIS conversation shows:
- "done": the conversation clearly shows it finished — he says he did it, the assistant confirms it shipped/works, the bug is fixed, the question is answered. Put the proof in status_reason (quote it).
- "open": the conversation shows it is still pending — raised but not resolved, deferred ("later", "next session"), or left hanging.
- "uncertain": the conversation does NOT make completion clear either way.
RULES: Never mark "done" on a guess or an assumption. If you are not sure, use "uncertain" — Khalil will be ASKED rather than misled (he would rather be asked than have you assume). Default to "open" or "uncertain". "done" requires explicit evidence you can quote. This mirrors his anti-sycophancy / anti-hallucination discipline: cite the evidence or admit uncertainty.

PEOPLE / REFERENCES / NUMBERS / FILES:
- people: any human name mentioned (not Khalil himself, not the assistant). Include with role/context.
- references: books, podcasts, tools, products, papers, courses mentioned BY NAME without URLs (URLs go in links[]).
- numbers: costs, percentages, headcount, deadlines, prices, metrics — any quantified fact worth anchoring.
- files: file paths referenced in conversation (e.g. ~/Documents/sanad/...). NOT URLs.

CONVERSATION:

"""


def extract_messages(jsonl_path: Path):
    messages = []
    with jsonl_path.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue
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
    return messages


def format_for_prompt(messages, max_messages=40):
    """Truncate to avoid argv blowup. 40 messages is plenty for the spine + sparks."""
    sample = messages[:max_messages]
    lines = []
    for i, m in enumerate(sample, 1):
        lines.append(f"--- msg_{i:02d} ({m['role']}) ---")
        lines.append(m["content"])
    return "\n".join(lines)


def regex_backstop_links(conversation_text: str) -> list:
    """Catch every literal URL in the raw conversation as a safety net."""
    patterns = [
        r"https?://[^\s\)\]\}\"\'>]+",
        r"\bgithub\.com/[\w.-]+/[\w.-]+",
        r"\b(?:x|twitter)\.com/[\w]+(?:/status/\d+)?",
    ]
    found = set()
    for pat in patterns:
        for m in re.findall(pat, conversation_text, flags=re.IGNORECASE):
            # Skip file paths and localhost
            if m.startswith(("file://", "http://localhost", "http://127.0.0.1", "https://localhost")):
                continue
            # Trim trailing punctuation
            m = m.rstrip(".,;:!?")
            if len(m) > 10:
                found.add(m)
    return sorted(found)


def merge_regex_links(card: dict, conversation_text: str) -> dict:
    """Add any literal URLs the LLM missed."""
    llm_urls = {l.get("url", "").rstrip(".,;:!?") for l in card.get("links", [])}
    backstop = regex_backstop_links(conversation_text)
    for url in backstop:
        if not any(url in lu or lu in url for lu in llm_urls if lu):
            card.setdefault("links", []).append({
                "url": url,
                "context": "(regex backstop — LLM extraction did not capture this URL with context)",
                "type": "other",
                "project": "none",
            })
    return card


def parse_card(raw_response: str) -> dict:
    """Strip fences if present, parse JSON, repair if malformed."""
    text = raw_response.strip()
    # Strip ```json ... ``` fences if model added them despite instruction
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    first = text.find("{")
    if first == -1:
        raise ValueError("No JSON object found in response")
    last = text.rfind("}")
    if last == -1:
        raise ValueError("No closing brace found")
    candidate = text[first:last + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to json-repair for common LLM JSON issues:
        # trailing commas, unescaped quotes inside strings, missing commas, etc.
        from json_repair import repair_json
        repaired = repair_json(candidate)
        return json.loads(repaired)


def pick_substantive_session():
    """Pick a session in the 150-400 line range with real substance."""
    candidates = []
    for p in CLAUDE_CODE_SESSIONS_DIR.glob("*.jsonl"):
        try:
            lines = sum(1 for _ in p.open())
            if 150 <= lines <= 400:
                candidates.append((lines, p))
        except OSError:
            continue
    candidates.sort()
    return candidates[len(candidates) // 2][1] if candidates else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session", nargs="?", help="session-id or full path to JSONL")
    parser.add_argument("--pick", action="store_true", help="auto-pick a substantive session")
    args = parser.parse_args()

    if args.pick or not args.session:
        path = pick_substantive_session()
        if not path:
            print("ERROR: no substantive sessions found"); sys.exit(1)
    else:
        # Try as path; fall back to session-id lookup
        path = Path(args.session)
        if not path.exists():
            path = CLAUDE_CODE_SESSIONS_DIR / f"{args.session}.jsonl"
            if not path.exists():
                print(f"ERROR: cannot find session {args.session}"); sys.exit(1)

    print(f">>> Session: {path.name}")
    messages = extract_messages(path)
    print(f">>> Messages: {len(messages)}")
    if not messages:
        print("ERROR: no user/assistant messages"); sys.exit(1)

    formatted = format_for_prompt(messages, max_messages=40)
    prompt = EXTRACTION_PROMPT + formatted

    # Inject user preferences + recent corrections + project descriptions
    prefs_block = format_preferences_for_prompt()
    corrections_block = format_corrections_for_prompt(n=20)

    # Use grouped parent projects (preferred) → flat registry → hardcoded fallback
    active = get_active_parent_projects() or get_active_projects()
    descriptions_block = format_descriptions_for_prompt(slugs=active) if active else ""

    prompt = prefs_block + corrections_block + descriptions_block + prompt

    if active:
        prompt = prompt.replace(
            "one of: " + ", ".join(KNOWN_PROJECTS),
            "one of: " + ", ".join(active),
        )

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    print(f">>> Preferences injected: {bool(prefs_block)}, corrections injected: {bool(corrections_block)}")
    print(f">>> Active projects from registry: {len(active) if active else 0}")
    print(f">>> Sending {len(prompt)} chars to LLM...")
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"])
    raw = llm.generate_response(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )

    # Semantic self-heal: if the extraction came back too thin given a substantive
    # source conversation, retry once with a stricter prompt before accepting it.
    source_len = sum(len(m.get("content", "")) for m in messages)
    if source_len > 800:
        try:
            preview_card = parse_card(raw)
            preview_card = merge_regex_links(preview_card, "\n".join(m["content"] for m in messages))
            opens = len(preview_card.get("open_items", []))
            sparks = len(preview_card.get("sparks", []))
            links = len(preview_card.get("links", []))
            if opens + sparks + links < 2:
                print(f">>> Self-heal: extraction looked thin ({opens} open, {sparks} sparks, {links} links). Retrying with stricter prompt...")
                retry_prompt = (
                    "The previous extraction returned very few items but the conversation was substantive. "
                    "Re-read it carefully. There ARE actionable items, sparks, or links you missed. "
                    "Be thorough. Output JSON only.\n\n"
                ) + prompt
                raw = llm.generate_response(
                    messages=[{"role": "user", "content": retry_prompt}],
                    response_format={"type": "json_object"},
                )
        except Exception:
            pass  # parsing failed during preview, let the main parse handle it

    print(">>> Raw response (first 500 chars):")
    print(raw[:500])
    print()

    print(">>> Parsing as card...")
    card = parse_card(raw)

    # Apply regex backstop for any URL the LLM missed
    raw_conversation = "\n".join(m["content"] for m in messages)
    card = merge_regex_links(card, raw_conversation)

    # Apply deterministic rules (Approach C) — non-bypassable
    card = apply_rules(card)

    print()
    print("=" * 60)
    print(json.dumps(card, indent=2, ensure_ascii=False))
    print("=" * 60)
    print()

    # Audit summary
    sparks_count = len(card.get("sparks", []))
    open_items_count = len(card.get("open_items", []))
    links_count = len(card.get("links", []))
    project = card.get("project_match", {}).get("id", "?")
    confidence = card.get("project_match", {}).get("confidence", 0)
    print(f">>> AUDIT:")
    print(f"    Topic length:     {len(card.get('topic', ''))} chars")
    print(f"    Context length:   {len(card.get('context', ''))} chars")
    print(f"    Project matched:  {project} ({confidence})")
    print(f"    Open items:       {open_items_count}")
    print(f"    Sparks captured:  {sparks_count}")
    print(f"    Links captured:   {links_count}")
    if sparks_count < 3:
        print(f"    ⚠ FEWER THAN 3 SPARKS — extraction may have missed mid-convo material")
    else:
        print(f"    ✓ At least 3 sparks captured")


if __name__ == "__main__":
    main()
