"""
Shared Phaedrus runtime config.

All Phaedrus storage lives under ~/.phaedrus/ to keep it isolated from
mem0's defaults (~/.mem0/ + /tmp/qdrant). Single source of truth.

LLM provider auto-selects based on what's available in the user's env:
- ANTHROPIC_API_KEY set → 'anthropic' direct API (fast, ~$0.02/session, vision-capable)
- else                  → 'codex_cli' (slow ~5-6min/session, free, text-only)

User can force a mode via PHAEDRUS_LLM_MODE=codex_cli|anthropic|openai.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from phaedrus-core root so API keys are visible at config-build time
_DOTENV_PATH = Path(__file__).parent.parent.parent / ".env"
if _DOTENV_PATH.exists():
    load_dotenv(_DOTENV_PATH, override=True)

PHAEDRUS_HOME = Path.home() / ".phaedrus"
PHAEDRUS_HOME.mkdir(parents=True, exist_ok=True)

QDRANT_PATH = PHAEDRUS_HOME / "qdrant"
HISTORY_DB_PATH = PHAEDRUS_HOME / "history.db"


def _pick_llm_config() -> dict:
    """Auto-select the best available LLM mode based on env."""
    forced = os.environ.get("PHAEDRUS_LLM_MODE", "").lower()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    if forced == "codex_cli":
        return {"provider": "codex_cli", "config": {"max_tokens": 2048}}

    if forced == "anthropic" or (not forced and anthropic_key):
        if not anthropic_key:
            raise RuntimeError("PHAEDRUS_LLM_MODE=anthropic but ANTHROPIC_API_KEY not set")
        return {
            "provider": "anthropic",
            "config": {
                "model": "claude-sonnet-4-5",
                "api_key": anthropic_key,
                "max_tokens": 2048,
            },
        }

    if forced == "openai" or (not forced and openai_key):
        if not openai_key:
            raise RuntimeError("PHAEDRUS_LLM_MODE=openai but OPENAI_API_KEY not set")
        return {
            "provider": "openai",
            "config": {
                "model": "gpt-5.5",
                "api_key": openai_key,
                "max_tokens": 2048,
            },
        }

    # Default fallback: codex_cli (free, slow, no API key needed)
    return {"provider": "codex_cli", "config": {"max_tokens": 2048}}


def get_llm_mode() -> str:
    """Return the active provider name for logging."""
    return _pick_llm_config()["provider"]


def build_config(collection: str = "phaedrus", embedding_dims: int = 384) -> dict:
    """Return a mem0 config dict with Phaedrus storage paths and the best available LLM."""
    return {
        "llm": _pick_llm_config(),
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
