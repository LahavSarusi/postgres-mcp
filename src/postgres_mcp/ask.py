"""The whole free-text-to-rows flow.

Describe the schema, have the LLM write SQL, run it read-only, return the rows.
When Postgres rejects the SQL (a wrong column name, a type mismatch), the error
goes back to the model once so it can correct itself; one retry fixes most such
mistakes, and a cap keeps a confused model from looping. The SQL is returned
with the rows so a wrong answer can be traced.
"""

from dataclasses import dataclass

import psycopg

from postgres_mcp.db import Row, run_read_only
from postgres_mcp.llm import generate_sql, start_conversation
from postgres_mcp.output import error_text
from postgres_mcp.schema import describe_schema


@dataclass
class AskResult:
    sql: str
    rows: list[Row]


async def ask(question: str) -> AskResult:
    messages = start_conversation(question, await describe_schema())
    sql = await generate_sql(messages)
    try:
        return AskResult(sql=sql, rows=await run_read_only(sql))
    except psycopg.OperationalError:
        # The connection failed or the query timed out: the SQL is not what is
        # wrong, so asking the model again would only cost another call.
        raise
    except psycopg.DatabaseError as error:  # Postgres rejected the SQL itself
        messages += [
            {"role": "assistant", "content": sql},
            {
                "role": "user",
                "content": (
                    f"PostgreSQL rejected that query: {error_text(error)}\n"
                    "Return a corrected query."
                ),
            },
        ]
        retry = await generate_sql(messages)
        return AskResult(sql=retry, rows=await run_read_only(retry))
