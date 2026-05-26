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
  "context": "2-3 sentences. What was being weighed, what trade-offs, what background was needed to make sense of the discussion?",
  "conclusion": "2-3 sentences. What got decided, what got committed, what was left open?",
  "project_match": {
    "id": "one of: """ + ", ".join(KNOWN_PROJECTS) + """ or 'none' or 'new'",
    "confidence": "0.0 to 1.0"
  },
  "open_items": [
    {
      "text": "Specific actionable. Direct quote or near-quote.",
      "type": "commitment | worry | idea | question",
      "project": "matched project id or 'none'",
      "due": "ISO date if explicit, else null"
    }
  ],
  "sparks": [
    {
      "text": "Quote the line. Capture EVERY actionable thought, every 'btw', every 'what if', every tangent. Scan all messages, not just start and end.",
      "trigger_context": "1 sentence: what surrounded this in the conversation",
      "project": "matched project id or 'none'",
      "type": "idea | observation | tangent | side-question"
    }
  ],
  "links": [
    {
      "url": "any URL mentioned in the conversation (https://..., github.com/..., x.com/..., twitter.com/...)",
      "context": "1 sentence: what was happening when this link came up, what it was for",
      "type": "github-repo | github-profile | tweet | x-profile | article | tool | reference | other",
      "project": "matched project id or 'none'"
    }
  ]
}

CRITICAL INSTRUCTIONS:
- Scan EVERY message from start to finish. The conversation may have sparks in the first message, the middle, or the end. All positions matter equally.
- Capture sparks even if the user did NOT follow up on them. They are valuable precisely because they were not pursued.
- If the conversation has any substance, MINIMUM 3 sparks. Empty sparks[] ONLY if the conversation is truly trivial (greeting, one-word reply, etc.).
- Use direct quotes from the conversation for sparks and open_items text where possible.
- For project_match: match against the user's known projects above. If the conversation does not relate to any, return id='none' with low confidence.
- For links: capture EVERY external URL mentioned anywhere in the conversation (https://..., http://..., github.com/..., x.com/..., twitter.com/...). Include the URL exactly as written, plus 1 sentence of surrounding context so Khalil can remember why it came up. Do NOT include file paths, localhost URLs, or internal tool URLs.

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
    """Strip fences if present, parse JSON."""
    text = raw_response.strip()
    # Strip ```json ... ``` fences if Codex added them despite instruction
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Find the first { and matching }
    first = text.find("{")
    if first == -1:
        raise ValueError("No JSON object found in response")
    # Naive: assume everything from first { to last } is the JSON
    last = text.rfind("}")
    if last == -1:
        raise ValueError("No closing brace found")
    return json.loads(text[first:last + 1])


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

    llm_conf = _pick_llm_config()
    print(f">>> LLM mode: {llm_conf['provider']}")
    print(f">>> Sending {len(prompt)} chars to LLM...")
    llm = LlmFactory.create(llm_conf["provider"], llm_conf["config"])
    raw = llm.generate_response(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )

    print(">>> Raw response (first 500 chars):")
    print(raw[:500])
    print()

    print(">>> Parsing as card...")
    card = parse_card(raw)

    # Apply regex backstop for any URL the LLM missed
    raw_conversation = "\n".join(m["content"] for m in messages)
    card = merge_regex_links(card, raw_conversation)

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
