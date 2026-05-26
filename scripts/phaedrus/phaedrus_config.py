"""
Shared Phaedrus runtime config.

All Phaedrus storage lives under ~/.phaedrus/ to keep it isolated from
mem0's defaults (~/.mem0/ + /tmp/qdrant). Single source of truth.
"""
from pathlib import Path

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(parents=True, exist_ok=True)

QDRANT_PATH = PHAEDRUS_HOME / "qdrant"
HISTORY_DB_PATH = PHAEDRUS_HOME / "history.db"


def build_config(collection: str = "phaedrus", embedding_dims: int = 384) -> dict:
    """Return a mem0 config dict with Phaedrus storage paths and codex_cli LLM."""
    return {
        "llm": {
            "provider": "codex_cli",
            "config": {"max_tokens": 2048},
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "BAAI/bge-small-en-v1.5",
                "embedding_dims": embedding_dims,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection,
                "embedding_model_dims": embedding_dims,
                "path": str(QDRANT_PATH),
                "on_disk": True,
            },
        },
        "history_db_path": str(HISTORY_DB_PATH),
    }
