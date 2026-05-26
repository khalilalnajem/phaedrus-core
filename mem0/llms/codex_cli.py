"""
Codex CLI LLM provider for mem0.

Phaedrus addition: shells out to the local Codex CLI (uses OAuth via the user's
ChatGPT/Codex subscription), so no API key burn. Trades latency for cost.

Apache 2.0 derived from mem0ai/mem0 patterns.
"""
import json
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Dict, List, Optional, Union

from mem0.configs.llms.base import BaseLlmConfig
from mem0.llms.base import LLMBase


def _messages_to_prompt(messages: List[Dict[str, str]]) -> str:
    """Serialise a chat-style message list into a single prompt string Codex can consume."""
    if not messages:
        return ""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        parts.append(f"[{role}]\n{content}".strip())
    return "\n\n".join(parts)


class CodexCLILLM(LLMBase):
    """LLM provider that wraps the `codex exec` CLI."""

    def __init__(self, config: Optional[Union[BaseLlmConfig, Dict]] = None):
        if config is None:
            config = BaseLlmConfig()
        elif isinstance(config, dict):
            config = BaseLlmConfig(**config)
        super().__init__(config)
        # codex_binary can be overridden by env or config; default to PATH lookup
        self.codex_binary = (
            getattr(config, "codex_binary", None)
            or os.environ.get("PHAEDRUS_CODEX_BINARY")
            or "codex"
        )

    def generate_response(
        self,
        messages: List[Dict[str, str]],
        response_format=None,
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> str:
        prompt = _messages_to_prompt(messages)

        # If caller wants JSON, prepend an explicit instruction so codex returns parseable JSON.
        wants_json = response_format and (
            (isinstance(response_format, dict) and response_format.get("type") == "json_object")
            or response_format == "json"
        )
        if wants_json:
            prompt = (
                "Return ONLY valid JSON in your final answer. No prose, no fences, no commentary.\n\n"
                + prompt
            )

        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out_file:
            out_path = out_file.name

        try:
            # Pass prompt via stdin instead of argv — mem0's extraction prompts
            # are multi-thousand chars and exceed shell ARG_MAX. `codex exec -`
            # reads instructions from stdin.
            cmd = [
                self.codex_binary,
                "exec",
                "--skip-git-repo-check",
                "--output-last-message",
                out_path,
                "-",
            ]
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed (rc={result.returncode}): {result.stderr[-500:]}"
                )

            response_text = Path(out_path).read_text().strip()
            if not response_text:
                raise RuntimeError("codex exec returned empty output")

            return response_text
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
