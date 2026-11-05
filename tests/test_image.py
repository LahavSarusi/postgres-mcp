"""The Docker image: builds, keeps secrets out, runs as non-root, serves MCP.

Slow, so opt-in: pytest --run-image
"""

import json
import os
import shutil
import subprocess
from collections.abc import Iterator

import anyio
import pytest
from conftest import PROJECT_ROOT, Database
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.image

IMAGE = "postgres-mcp-python:pytest"


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    if not shutil.which("docker"):
        pytest.skip("Docker is not installed")
    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, str(PROJECT_ROOT)],
        check=True, capture_output=True, timeout=900,
    )  # fmt: skip
    yield IMAGE
    subprocess.run(["docker", "rmi", "-f", IMAGE], capture_output=True, timeout=120)


def sh(image: str, command: str) -> str:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", command],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()  # fmt: skip


def test_image_has_no_secrets_and_runs_as_non_root(image: str) -> None:
    assert sh(image, "ls -A /app").split() == ["README.md", "pyproject.toml", "src"]
    assert sh(image, "id -u") == "1000"
    assert sh(image, "command -v postgres-mcp-server") != ""


def test_image_serves_mcp(image: str, database: Database) -> None:
    if database.container is None:
        pytest.skip("needs the Docker-started test database (TEST_DATABASE_URL is set)")

    async def check() -> None:
        args = [
            "run", "-i", "--rm", "--network", f"container:{database.container}",
            "-e", "PGHOST=127.0.0.1", "-e", "PGPORT=5432",
            "-e", f"PGUSER={database.user}", "-e", f"PGPASSWORD={database.password}",
            "-e", f"PGDATABASE={database.dbname}", "-e", "PGSSLMODE=", image,
        ]  # fmt: skip
        params = StdioServerParameters(command="docker", args=args, env=dict(os.environ))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "query", {"sql": "SELECT name FROM customers ORDER BY id"}
            )
            first = result.content[0]
            assert isinstance(first, types.TextContent)
            assert json.loads(first.text)[0] == {"name": "דנה כהן"}
            result = await session.call_tool("ask", {"question": "q"})
            assert result.is_error  # no LLM configured in the container
            message = result.content[0]
            assert isinstance(message, types.TextContent)
            assert message.text.startswith("Missing LLM settings")

    anyio.run(check)
