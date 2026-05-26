import os
from dotenv import load_dotenv

# override=True because Claude Code sets ANTHROPIC_API_KEY="" in our shell
load_dotenv("/Users/khalilalnajem/Documents/phaedrus-core/.env", override=True)

key = os.environ.get("ANTHROPIC_API_KEY", "")
print(f">>> Key loaded: {bool(key)}, length: {len(key)}")

from mem0 import Memory

config = {
    "llm": {
        "provider": "anthropic",
        "config": {
            "model": "claude-sonnet-4-5",
            "api_key": key,
            "max_tokens": 1024
        }
    },
    "embedder": {
        "provider": "fastembed",
        "config": {
            "model": "BAAI/bge-small-en-v1.5",
            "embedding_dims": 384
        }
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "phaedrus_test",
            "embedding_model_dims": 384,
            "on_disk": False
        }
    }
}

print(">>> Building Memory instance...")
m = Memory.from_config(config)
print(">>> Memory ready.")
print("")

print(">>> ADD: 'I am Khalil. I prefer Claude over GPT for technical work. I am building Phaedrus.'")
result = m.add(
    "I am Khalil. I prefer Claude over GPT for technical work. I am building Phaedrus.",
    user_id="khalil"
)
print(">>> ADD result:")
print(result)
print("")

print(">>> SEARCH: 'what does Khalil prefer?'")
results = m.search("what does Khalil prefer?", filters={"user_id": "khalil"})
print(">>> SEARCH results:")
print(results)
print("")

print(">>> ADD: 'I am forking mem0 and naming the result Phaedrus. The product is an action layer on top of cross-AI capture.'")
result2 = m.add(
    "I am forking mem0 and naming the result Phaedrus. The product is an action layer on top of cross-AI capture.",
    user_id="khalil"
)
print(">>> ADD result:")
print(result2)
print("")

print(">>> SEARCH: 'what is Phaedrus?'")
results2 = m.search("what is Phaedrus?", filters={"user_id": "khalil"})
print(">>> SEARCH results:")
print(results2)
