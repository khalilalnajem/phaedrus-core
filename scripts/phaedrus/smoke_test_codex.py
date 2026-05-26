"""Phaedrus smoke test: codex_cli LLM + fastembed + qdrant on ~/.phaedrus/."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phaedrus_config import build_config, PHAEDRUS_HOME, QDRANT_PATH, HISTORY_DB_PATH

from mem0 import Memory

print(f">>> Phaedrus storage: {PHAEDRUS_HOME}")
print(f"    qdrant: {QDRANT_PATH}")
print(f"    history: {HISTORY_DB_PATH}")
print()

m = Memory.from_config(build_config())
print(">>> Memory ready.")
print()

print(">>> ADD: 'I am Khalil. Phaedrus uses Codex via OAuth so it does not burn API credits.'")
result = m.add(
    "I am Khalil. Phaedrus uses Codex via OAuth so it does not burn API credits.",
    user_id="khalil",
)
print(json.dumps(result, indent=2, default=str)[:1200])
print()

print(">>> SEARCH: 'how does Phaedrus authenticate?'")
r = m.search("how does Phaedrus authenticate?", filters={"user_id": "khalil"})
for hit in r.get("results", [])[:5]:
    print(f"    [{hit['score']:.3f}] {hit['memory'][:200]}")
