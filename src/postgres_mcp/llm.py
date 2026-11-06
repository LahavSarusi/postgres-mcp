"""Turns a free-text question into one PostgreSQL query by calling the configured LLM.

The model is reached through the OpenAI-compatible chat-completions API, which
most model hosts serve, using httpx so no vendor SDK is needed. temperature 0
keeps the same question producing the same SQL. Only the SQL is asked for: the
rows are returned to the caller as they are, so no model runs after the query.
"""

import asyncio
import re
from typing import Literal, TypedDict

import httpx
from pydantic import BaseModel, Field, ValidationError

from postgres_mcp.config import llm_config
from postgres_mcp.output import error_text

SYSTEM_PROMPT = """You translate questions about a PostgreSQL database into SQL.

Rules:
- Write exactly one PostgreSQL SELECT query (a WITH ... SELECT is fine).
- Use only the tables and columns listed in the schema. Never invent names.
- Write identifiers exactly as the schema shows them, including any quotes.
- Lines starting with "--" under a table describe that table or one of its columns.
  Use them to choose tables and columns and to pick the exact literal values to compare.
- The question may be in any language, Hebrew or English included; the SQL is the same.
- Text values in the question may be in a different language or spelling than in the
  data; prefer ILIKE for matching names and free text.
- Return only the SQL. No explanation, no markdown fences."""

REQUEST_TIMEOUT_S = 120


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class LlmError(RuntimeError):
    """The LLM could not be reached or gave no usable answer."""


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:sql)?", re.IGNORECASE)


def extract_sql(reply: str) -> str:
    """Extracts the SQL from a model reply that may add reasoning or fences."""
    return _FENCE.sub("", _THINK.sub("", reply)).strip()


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _Message


class _Completion(BaseModel):
    """The part of a chat-completions reply that is used: choices[0].message.content."""

    choices: list[_Choice] = Field(min_length=1)


async def generate_sql(messages: list[ChatMessage]) -> str:
    """Sends the conversation and returns the SQL the model wrote."""
    config = llm_config()
    url = f"{config.base_url}/chat/completions"
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT_S),
            httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, follow_redirects=True) as client,
        ):
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json={"model": config.model, "messages": messages, "temperature": 0},
            )
    except TimeoutError as error:
        raise LlmError(
            f"Could not reach the LLM at {url}: no reply within {REQUEST_TIMEOUT_S} s"
        ) from error
    except httpx.HTTPError as error:
        # The reason (DNS, refused, TLS, timeout) is what helps; say it.
        raise LlmError(f"Could not reach the LLM at {url}: {error_text(error)}") from error

    if not response.is_success:
        raise LlmError(f"LLM request failed ({response.status_code}): {response.text[:500]}")
    try:
        content = _Completion.model_validate_json(response.content).choices[0].message.content
    except ValidationError as error:
        if error.errors()[0]["type"] == "json_invalid":
            raise LlmError(
                f"LLM returned a reply that is not JSON: {response.text[:500]}"
            ) from error
        raise LlmError("LLM returned no content") from error
    if not content:
        raise LlmError("LLM returned no content")
    sql = extract_sql(content)
    if not sql:
        raise LlmError("LLM returned no SQL")
    return sql


def start_conversation(question: str, schema: str) -> list[ChatMessage]:
    """The opening messages for a question: the rules, the schema, the question."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Schema:\n{schema}\n\nQuestion: {question}"},
    ]
