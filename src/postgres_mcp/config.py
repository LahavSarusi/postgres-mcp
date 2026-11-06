"""Loads the project's .env file and exposes the settings the other modules need.

It is imported before anything that reads the environment, so every entry point
(the MCP server and the CLI) sees the same configuration. The file is resolved
from the project root rather than the current directory, because MCP clients
launch the server from a directory of their own choosing; variables already set
in the environment take precedence over the file.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# src/postgres_mcp/config.py -> the project root, two levels above the package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    # interpolate=False: a password containing "${...}" is taken literally.
    load_dotenv(env_file, override=False, interpolate=False, encoding="utf-8")


def _positive_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = int(raw)  # int() ignores surrounding whitespace
    except ValueError:
        value = 0
    if value <= 0:
        print(f'{name} must be a positive whole number, got "{raw}".', file=sys.stderr)
        sys.exit(1)
    return value


max_rows = _positive_int("MAX_ROWS", 500)
"""Most rows a single query may return."""

query_timeout_ms = _positive_int("QUERY_TIMEOUT_MS", 15000)
"""Postgres statement_timeout for each query, in milliseconds."""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str


def llm_config() -> LlmConfig:
    """The LLM settings.

    Read on demand rather than at startup, so the MCP server's `query` tool and
    schema resources keep working when no LLM is configured.
    """
    missing = [
        name for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Missing LLM settings: {', '.join(missing)}. Set them in {env_file}.")
    return LlmConfig(
        base_url=os.environ["LLM_BASE_URL"].rstrip("/"),
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ["LLM_MODEL"],
    )
