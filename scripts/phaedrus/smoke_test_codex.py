import os, json
from mem0 import Memory

config = {
    "llm": {
        "provider": "codex_cli",
        "config": {
            "model": "codex-default",
            "max_tokens": 1024
        }
    },
    "embedder": {
        "provider": "fastembed",
        "config": {"model": "BAAI/bge-small-en-v1.5", "embedding_dims": 384}
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {"collection_name": "phaedrus", "embedding_model_dims": 384, "on_disk": False}
    }
}

print(">>> Building Memory with codex_cli LLM provider...")
m = Memory.from_config(config)
print(">>> Ready.")
print()

print(">>> ADD: 'I am Khalil. I am building Phaedrus, which uses Codex via OAuth so it does not burn API credits.'")
result = m.add(
    "I am Khalil. I am building Phaedrus, which uses Codex via OAuth so it does not burn API credits.",
    user_id="khalil"
)
print(">>> ADD result:")
print(json.dumps(result, indent=2, default=str)[:1500])
print()

print(">>> SEARCH: 'how does Phaedrus authenticate?'")
r = m.search("how does Phaedrus authenticate?", filters={"user_id": "khalil"})
for hit in r.get("results", [])[:3]:
    print(f"    [{hit['score']:.3f}] {hit['memory'][:200]}")
