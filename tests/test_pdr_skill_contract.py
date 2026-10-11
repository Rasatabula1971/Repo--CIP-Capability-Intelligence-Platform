"""
The coding skill and the MCP server must describe the same workflow.

Spec v1.1 §9: the lock binds only because the driving LLM is told to
route its work through CIP's tools. The skill is where it is told. v1.0
never built the skill, so nothing checked that the instructions and the
tools agreed. This is that check — mechanical, but it fails the build
when a tool is renamed and the skill is not, or the other way round.

It cannot test that an agent follows the skill. The runtime signal for
that is in coverage().compliance.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from mcp_server import server

SKILL = (
    Path(__file__).parent.parent
    / "plugins" / "cip-composer" / "skills" / "cip-pdr-build" / "SKILL.md"
)

# The tools that make up the PDR-to-Build workflow. A new one must be
# taught in the skill before this list can grow.
WORKFLOW_TOOLS = {
    "pdr_status", "ingest_pdr", "record_requirements", "record_capability_needs",
    "retire_requirement", "request_requirements_confirmation",
    "search_for_requirement", "record_decision", "pdr_approval_brief",
    "lock_architecture", "check_against_lock", "record_build_progress",
    "pdr_coverage",
}


@pytest.fixture(scope="module")
def registered() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_exists_with_frontmatter(skill_text):
    assert skill_text.startswith("---\nname: cip-pdr-build\n")
    assert "\ndescription: " in skill_text.split("---")[1]


def test_every_tool_the_skill_names_exists(skill_text, registered):
    named = set(re.findall(r"mcp__cip__([a-z_]+)", skill_text))
    assert named, "the skill names no tools"
    assert named <= registered, f"skill names tools the server lacks: {named - registered}"


def test_every_workflow_tool_is_registered_and_taught(skill_text, registered):
    assert WORKFLOW_TOOLS <= registered, WORKFLOW_TOOLS - registered
    named = set(re.findall(r"mcp__cip__([a-z_]+)", skill_text))
    assert WORKFLOW_TOOLS <= named, f"skill does not teach: {WORKFLOW_TOOLS - named}"


def test_no_mcp_tool_can_record_an_approval(registered):
    """Human approval has exactly one channel, and it is not a tool."""
    suspicious = {n for n in registered if "approv" in n and n != "pdr_approval_brief"}
    assert suspicious == set(), suspicious
    for tool in asyncio.run(server.mcp.list_tools()):
        if tool.name in WORKFLOW_TOOLS:
            props = (tool.inputSchema or {}).get("properties", {})
            assert "approved_by" not in props, f"{tool.name} takes a free-text approver"
            assert "approver" not in props, f"{tool.name} takes a free-text approver"


def test_skill_tells_the_agent_not_to_approve(skill_text):
    assert "You never approve anything" in skill_text
    assert "Never run `cip-composer approvals approve`" in skill_text


def test_skill_states_the_limitation_honestly(skill_text):
    """§9: do not describe the lock as enforcement it is not."""
    assert "Nothing physically stops code being written outside them" in skill_text
    assert "is not built" in skill_text


def test_skill_teaches_the_cli_commands_that_exist(skill_text):
    from cip_composer import cli
    parser = cli._build_parser()
    for command in re.findall(r"cip-composer ([a-z-]+)", skill_text):
        # parse_known_args exits on an unknown subcommand.
        try:
            parser.parse_known_args([command, "--help"])
        except SystemExit as e:
            assert e.code == 0, f"skill mentions unknown command: cip-composer {command}"
