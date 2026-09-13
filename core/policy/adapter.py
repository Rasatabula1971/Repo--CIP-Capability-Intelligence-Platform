"""
Adapter skeleton generator (Phase 9).

Produces Python code skeletons for bridging two components that have
an 'adapter_needed' compatibility verdict. The skeleton is a starting
point — the operator fills in the domain-specific transformation.

Bridge kinds:
  subprocess     — source is a CLI, target consumes its stdout
  http           — source or target is an HTTP endpoint
  mcp_client     — source/target communicates via MCP protocol
  type_transform — same runtime but I/O types don't match
  pip_install    — git_clone source needs pip install -e
  skill_invoke   — Claude skill/agent invokes another component
  generic        — fallback for unknown bridge patterns

Pure domain logic — no DB, no IO.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterSkeleton:
    bridge_kind: str
    source_runtime: str
    target_runtime: str
    adapter_code: str
    test_code: str
    description: str


def classify_bridge(
    source_runtime: str,
    target_runtime: str,
    adapter_hint: str = "",
) -> str:
    """Map a runtime pair to a bridge_kind."""
    pair = (source_runtime, target_runtime)

    if "subprocess" in adapter_hint.lower() or pair in (
        ("python_import", "cli_subprocess"),
        ("cli_subprocess", "python_import"),
    ):
        return "subprocess"

    if "http" in adapter_hint.lower() or "fetch" in adapter_hint.lower() or pair in (
        ("python_import", "http_endpoint"),
        ("http_endpoint", "python_import"),
        ("npm_import", "http_endpoint"),
        ("http_endpoint", "npm_import"),
        ("cli_subprocess", "http_endpoint"),
        ("http_endpoint", "cli_subprocess"),
    ):
        return "http"

    if "mcp" in adapter_hint.lower() or pair in (
        ("python_import", "mcp_stdio"),
        ("mcp_stdio", "python_import"),
        ("claude_skill", "mcp_stdio"),
        ("claude_agent", "mcp_stdio"),
    ):
        return "mcp_client"

    if pair in (
        ("git_clone", "python_import"),
    ):
        return "pip_install"

    if pair in (
        ("git_clone", "cli_subprocess"),
    ):
        return "subprocess"

    if pair in (
        ("claude_skill", "python_import"),
        ("claude_agent", "python_import"),
    ):
        return "skill_invoke"

    if source_runtime == target_runtime:
        return "type_transform"

    return "generic"


def generate_skeleton(
    bridge_kind: str,
    source_name: str = "source",
    target_name: str = "target",
    source_runtime: str = "",
    target_runtime: str = "",
    io_transform: dict[str, Any] | None = None,
) -> AdapterSkeleton:
    """
    Generate a code skeleton for the given bridge kind.
    Returns adapter code + contract test code.
    """
    gen = _GENERATORS.get(bridge_kind, _generic_skeleton)
    return gen(
        bridge_kind=bridge_kind,
        source_name=source_name,
        target_name=target_name,
        source_runtime=source_runtime,
        target_runtime=target_runtime,
        io_transform=io_transform or {},
    )


# ---------------------------------------------------------------------------
# Per-bridge generators
# ---------------------------------------------------------------------------

def _subprocess_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} -> {tgt} via subprocess."""
import json
import subprocess


def adapt(input_data):
    """
    Call {src} as a subprocess and transform output for {tgt}.

    TODO: Replace the command and parsing with actual values.
    """
    result = subprocess.run(
        ["{src}", "--input", json.dumps(input_data)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_returns_dict():
    # TODO: Replace with realistic input
    result = adapt({{"key": "value"}})
    assert isinstance(result, dict)


def test_adapt_handles_empty_input():
    result = adapt({{}})
    assert result is not None
'''
    return AdapterSkeleton(
        bridge_kind="subprocess",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"Subprocess bridge: call {src} CLI, parse stdout for {tgt}",
    )


def _http_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} -> {tgt} via HTTP."""
import httpx


def adapt(input_data, base_url="http://localhost:8000"):
    """
    Call {src} HTTP endpoint and transform response for {tgt}.

    TODO: Replace URL path and response parsing.
    """
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{{base_url}}/api/process",
            json=input_data,
        )
        response.raise_for_status()
        return response.json()
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} HTTP adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_calls_endpoint(httpx_mock):
    # TODO: Set up mock response
    httpx_mock.add_response(json={{"result": "ok"}})
    result = adapt({{"key": "value"}})
    assert result["result"] == "ok"
'''
    return AdapterSkeleton(
        bridge_kind="http",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"HTTP bridge: POST to {src} endpoint, parse response for {tgt}",
    )


def _mcp_client_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} -> {tgt} via MCP."""
import asyncio
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def adapt_async(input_data, server_command="{src}"):
    """
    Call {src} MCP tool and transform result for {tgt}.

    TODO: Replace tool_name and argument mapping.
    """
    params = StdioServerParameters(command=server_command)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "process",
                arguments=input_data,
            )
            return result


def adapt(input_data, server_command="{src}"):
    return asyncio.run(adapt_async(input_data, server_command))
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} MCP adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_signature():
    # Verify the adapter function exists and accepts input_data
    assert callable(adapt)
'''
    return AdapterSkeleton(
        bridge_kind="mcp_client",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"MCP bridge: call {src} tool via stdio, transform for {tgt}",
    )


def _type_transform_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    io = kw.get("io_transform", {})
    src_type = io.get("source_output", "Any")
    tgt_type = io.get("target_input", "Any")
    code = f'''\
"""Adapter: {src} -> {tgt} type transform."""


def adapt(output_data):
    """
    Transform {src} output ({src_type}) to {tgt} input ({tgt_type}).

    TODO: Implement the actual field mapping.
    """
    return {{
        # Map fields from source output to target input
        # "target_field": output_data["source_field"],
    }}
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} type transform."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_transforms_types():
    source_output = {{"example": "data"}}
    result = adapt(source_output)
    assert isinstance(result, dict)


def test_adapt_preserves_required_fields():
    # TODO: Check that target's required fields are present
    result = adapt({{"example": "data"}})
    assert result is not None
'''
    return AdapterSkeleton(
        bridge_kind="type_transform",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"Type transform: convert {src} output to {tgt} input format",
    )


def _pip_install_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} -> {tgt} via pip install from clone."""
import subprocess
import tempfile
from pathlib import Path


def adapt(repo_url, import_name="{src}"):
    """
    Clone {src}, pip install -e, then import for {tgt}.

    TODO: Replace repo_url and import_name.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dir = Path(tmpdir) / "repo"
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(clone_dir)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["pip", "install", "-e", str(clone_dir)],
            check=True, capture_output=True,
        )
        import importlib
        mod = importlib.import_module(import_name)
        return mod
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} pip install adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_signature():
    assert callable(adapt)
'''
    return AdapterSkeleton(
        bridge_kind="pip_install",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"Pip install bridge: clone {src}, install editable, import for {tgt}",
    )


def _skill_invoke_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} skill/agent -> {tgt} via Python import."""


def adapt(input_data, module_name="{tgt}"):
    """
    Import {tgt} and call its entry point from {src} skill/agent.

    TODO: Replace module_name and function call.
    """
    import importlib
    mod = importlib.import_module(module_name)
    return mod.process(input_data)
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} skill invoke adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_signature():
    assert callable(adapt)
'''
    return AdapterSkeleton(
        bridge_kind="skill_invoke",
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"Skill invoke: {src} calls {tgt} via Python import",
    )


def _generic_skeleton(**kw) -> AdapterSkeleton:
    src = kw["source_name"]
    tgt = kw["target_name"]
    code = f'''\
"""Adapter: {src} -> {tgt} (generic bridge)."""


def adapt(input_data):
    """
    Bridge {src} output to {tgt} input.

    TODO: This is a generic skeleton. Determine the actual
    communication mechanism and implement accordingly.
    """
    raise NotImplementedError(
        "Generic adapter — replace with actual bridge logic"
    )
'''
    test = f'''\
"""Contract tests for {src} -> {tgt} generic adapter."""
import pytest
from adapter_{src}_{tgt} import adapt


def test_adapt_exists():
    assert callable(adapt)


def test_adapt_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        adapt({{}})
'''
    return AdapterSkeleton(
        bridge_kind=kw["bridge_kind"],
        source_runtime=kw["source_runtime"],
        target_runtime=kw["target_runtime"],
        adapter_code=code,
        test_code=test,
        description=f"Generic bridge: {src} -> {tgt} (needs manual implementation)",
    )


_GENERATORS = {
    "subprocess": _subprocess_skeleton,
    "http": _http_skeleton,
    "mcp_client": _mcp_client_skeleton,
    "type_transform": _type_transform_skeleton,
    "pip_install": _pip_install_skeleton,
    "skill_invoke": _skill_invoke_skeleton,
    "generic": _generic_skeleton,
}
