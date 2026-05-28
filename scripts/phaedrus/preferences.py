"""
Phaedrus preferences page.

Plain-English rules the user writes once. Phaedrus reads them on every
extraction and includes them in the prompt. Lowest-friction personalisation.

File: ~/.phaedrus/preferences.md
"""
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(exist_ok=True)
PREFS_PATH = PHAEDRUS_HOME / "preferences.md"

DEFAULT_TEMPLATE = """# Phaedrus Preferences

Write any rules you want Phaedrus to follow when extracting cards from your conversations.
Plain English. One rule per line. Phaedrus reads this file before every extraction.

## Examples (edit or delete these)

- Always tag conversations about Aisha or my MOHE scholarship as `personal`, not work.
- Treat Sanad and sanad-mvp as the same project.
- Don't extract commitments older than 6 months — they're stale.
- If a chat mentions Valencourt, project is always `valencourt`.
- Books, podcasts, and tools I mention in passing are sparks, not commitments.
- Group commitments about "writing" under the `writing-kit` project.

## My rules

(Add your own below)

"""


def ensure_default_preferences() -> Path:
    """Create the preferences file with a default template if it doesn't exist."""
    if not PREFS_PATH.exists():
        PREFS_PATH.write_text(DEFAULT_TEMPLATE)
    return PREFS_PATH


def load_preferences() -> str:
    """Return the user's preferences as a string to be injected into prompts."""
    ensure_default_preferences()
    text = PREFS_PATH.read_text().strip()
    return text


def format_preferences_for_prompt() -> str:
    """Return preferences wrapped for prompt injection. Empty string if user has only defaults."""
    raw = load_preferences()
    # Strip the example block so we don't feed examples back as if they were rules
    if "## My rules" in raw:
        my_rules = raw.split("## My rules", 1)[1].strip()
        if my_rules and not my_rules.startswith("(Add"):
            return (
                "USER PREFERENCES (the user wrote these — always follow them):\n"
                + my_rules
                + "\n\n"
            )
    return ""


if __name__ == "__main__":
    p = ensure_default_preferences()
    print(f"Preferences file: {p}")
    print(f"Edit it in your favorite editor, then re-run extraction.")
    formatted = format_preferences_for_prompt()
    if formatted:
        print(f"\n--- Current rules (will be injected) ---\n{formatted}")
    else:
        print(f"\n(No user rules yet — only defaults)")
