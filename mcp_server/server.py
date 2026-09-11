"""
CIP MCP server.

Exposes the capability registry as MCP tools an LLM can call. Uses the
official Python MCP SDK's FastMCP wrapper. Transport: stdio, which is
what Claude Code and Claude Desktop expect.

Registering with Claude Code:

    claude mcp add cip -- cip-mcp

Or, if the console script is not on PATH:

    claude mcp add cip -- python -m mcp_server.server

Environment: reads DATABASE_URL from .env in the current working
directory, same as everything else in this repo.
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from analysis.extractors import extractor_names as _extractor_names
from db.connection import connect
from mcp_server import queries


mcp = FastMCP("cip")


@mcp.tool()
def search_capabilities(
    query: str,
    ecosystem: Optional[str] = None,
    capability_kind: Optional[str] = None,
    component_kind: Optional[str] = None,
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    project_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search the CIP capability registry with relevance ranking.

    Uses trigram similarity for fuzzy matching (typo-tolerant), full-text
    search on descriptions, and composite relevance scoring. Falls back
    to substring matching when pg_trgm is not available.

    Args:
      query: search terms matched against display_name, normalized_key,
        and description fields.
      ecosystem: optional — 'pypi', 'npm', 'source'.
      capability_kind: optional semantic role — 'library', 'cli', 'service', ...
      component_kind: optional format — 'library', 'repo', 'agent', 'skill',
        'mcp_tool', 'workflow_template'.
      runtime: optional — 'python_import', 'mcp_stdio', 'claude_skill',
        'git_clone', ...
      cost_tier: optional — 'free', 'free_tier', 'cheap_paid', 'paid'.
      project_id: optional UUID — when set, hard-filters rows that fail
        any of the project's constraints (cpu_only, must_be_free_tier,
        must_be_local, license_allowlist, etc.). Kept rows include a
        `constraint_verdicts` field showing per-constraint outcomes.
      tags: optional list of topic strings — rows must contain ALL
        specified tags in their metadata.topics array.
      limit: max rows (1-100, default 20).

    Returns rows with {id, normalized_key, display_name, ecosystem,
    capability_kind, component_kind, runtime, cost_tier, license_spdx,
    head_version_id, display_version, total_score, confidence,
    text_relevance}. Ranked by composite score: 50% text relevance,
    35% intrinsic score, 15% recency.
    """
    conn = connect()
    try:
        return queries.search_capabilities(
            conn,
            query=query,
            ecosystem=ecosystem,
            capability_kind=capability_kind,
            component_kind=component_kind,
            runtime=runtime,
            cost_tier=cost_tier,
            project_id=project_id,
            tags=tags,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def browse_components(
    component_kind: Optional[str] = None,
    ecosystem: Optional[str] = None,
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Discovery tool: enumerate components without a keyword. Use when
    the caller doesn't know what to search for and wants to see what
    exists in a category.

    Args:
      component_kind: 'library' | 'repo' | 'agent' | 'skill' | 'mcp_tool'
        | 'workflow_template'.
      ecosystem: 'pypi', 'npm', 'source', ...
      runtime: 'python_import', 'mcp_stdio', 'claude_skill', ...
      cost_tier: 'free', 'free_tier', 'cheap_paid', 'paid'.
      limit: max rows (1-500, default 50).

    Returns the same shape as search_capabilities. With no filters,
    returns the whole registry (up to `limit`) ranked by intrinsic score.
    """
    conn = connect()
    try:
        return queries.browse_components(
            conn,
            component_kind=component_kind,
            ecosystem=ecosystem,
            runtime=runtime,
            cost_tier=cost_tier,
            project_id=project_id,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_constraint_fit(
    project_id: str,
    capability_id: str,
) -> Optional[dict[str, Any]]:
    """
    Full per-constraint verdict for one component against one project's
    project_constraint set.

    Args:
      project_id:    UUID of the project row.
      capability_id: UUID of the capability row.

    Returns {capability_id, normalized_key, display_name, hard_fail,
    verdicts:[{kind, passed, reason, detail}]} or None if either id
    is malformed or not found. If the project has no constraints,
    hard_fail is False and verdicts is [].
    """
    conn = connect()
    try:
        return queries.capability_constraint_fit(
            conn, project_id=project_id, capability_id=capability_id,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_compatibility(
    source_id: str,
    target_id: str,
) -> Optional[dict[str, Any]]:
    """
    Evaluate whether the source component can feed the target
    downstream. Reads runtimes and (when populated) the head-version
    interface I/O types.

    Args:
      source_id: UUID of the upstream/producing capability.
      target_id: UUID of the downstream/consuming capability.

    Returns {source, target, verdict, reason, detail, io_type_check,
    adapter_hint} or None on malformed / missing ids.

    verdict is one of:
      compatible      — same runtime family, I/O types match or absent
      adapter_needed  — different family with a known bridge, OR same
                        family with mismatched types
      incompatible    — no known bridge between the runtimes
    """
    conn = connect()
    try:
        return queries.capability_compatibility(
            conn, source_id=source_id, target_id=target_id,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_license_check(
    capability_id: str,
    project_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Check a capability's license status.

    Returns the license classification: whether it's safe to reuse,
    needs human review, or is blocked. Optionally applies a project's
    license policy profile.

    Args:
      capability_id: UUID of the capability row.
      project_id: optional UUID — when set, applies the project's
        license_policy_profile (allowed/denied SPDX lists).

    Returns {capability_id, spdx_id, status, category, reason,
    obligations} or None if the id is malformed/not found.

    status is one of:
      verified_open_source — permissive or weak-copyleft; safe to reuse
      needs_review         — copyleft or uncommon; human decision needed
      blocked              — no license or explicitly denied
      unknown              — not yet evaluated
    """
    conn = connect()
    try:
        return queries.capability_license_check(
            conn,
            capability_id=capability_id,
            project_id=project_id,
        )
    finally:
        conn.close()


@mcp.tool()
def dependency_fit(
    capability_id: str,
    project_id: Optional[str] = None,
    profile_name: str = "default",
) -> Optional[dict[str, Any]]:
    """
    Check whether a capability's dependencies are satisfiable in a
    project's target environment.

    Examines runtime version requirements, OS constraints, service
    dependencies, system libraries, environment variables, and hardware
    needs. Also lists the capability's package-level dependencies.

    Args:
      capability_id: UUID of the capability to check.
      project_id: optional UUID — when set, checks against that project's
        environment_profile.
      profile_name: which environment profile to use (default: 'default').

    Returns {capability_id, normalized_key, passed, hard_failures[],
    warnings[], package_deps[]} or None if the id is malformed/not found.

    passed is False when any hard failure exists — the capability
    cannot run in the target environment. Warnings indicate missing
    profile data or optional dependencies.
    """
    conn = connect()
    try:
        return queries.dependency_fit(
            conn,
            capability_id=capability_id,
            project_id=project_id,
            profile_name=profile_name,
        )
    finally:
        conn.close()


@mcp.tool()
def dependency_conflicts(
    capability_id_a: str,
    capability_id_b: str,
) -> Optional[dict[str, Any]]:
    """
    Detect package-level version conflicts between two capabilities.

    Compares declared package dependencies and flags any where both
    capabilities require the same package with incompatible version ranges.

    Args:
      capability_id_a: UUID of the first capability.
      capability_id_b: UUID of the second capability.

    Returns {capability_a, capability_b, conflicts[]} where each
    conflict has {ecosystem, name, spec_a, spec_b, reason}.
    Empty conflicts list means no detected conflicts.
    """
    conn = connect()
    try:
        return queries.dependency_conflicts(
            conn,
            capability_id_a=capability_id_a,
            capability_id_b=capability_id_b,
        )
    finally:
        conn.close()


@mcp.tool()
def search_symbols(
    query: str,
    symbol_kind: Optional[str] = None,
    role: Optional[str] = None,
    language: Optional[str] = None,
    capability_id: Optional[str] = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """
    Search for symbols (functions, classes, methods) across the registry.

    Finds reusable functions and classes, not just whole repositories.
    Returns symbol-level results with module path, signature, role
    classification, and the owning capability.

    Args:
      query: search terms matched against symbol_name and qualified_name.
      symbol_kind: optional filter — 'function', 'async_function', 'class',
        'method', 'async_method', 'constant', 'module', 'interface',
        'type_alias', 'enum'.
      role: optional filter — 'entry_point', 'utility', 'data_model',
        'cli_command', 'api_endpoint', 'decorator', 'factory',
        'middleware', 'exception', 'test_helper', 'component', 'hook',
        'guard', 'pipe', 'enum_type'.
      language: optional filter — 'python', 'typescript', 'javascript'.
      capability_id: optional UUID — restrict to symbols from one capability.
      limit: max rows (1-200, default 30).

    Returns rows with {symbol_id, module_path, symbol_kind, symbol_name,
    qualified_name, signature, return_type, docstring_summary, role,
    language, capability_id, normalized_key, display_name, display_version}.
    """
    conn = connect()
    try:
        return queries.search_symbols(
            conn,
            query=query,
            symbol_kind=symbol_kind,
            role=role,
            language=language,
            capability_id=capability_id,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def ingest_symbols(
    capability_version_id: str,
    source_revision_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Materialize symbol evidence into searchable capability_symbol rows.

    Reads 'symbol' evidence items produced by the Python SymbolExtractor
    or TypeScript TSSymbolExtractor and inserts them into capability_symbol
    for use by search_symbols. Skips duplicates by qualified_name.

    Args:
      capability_version_id: UUID of the capability version to populate.
      source_revision_id: optional UUID — scope to evidence from one revision.

    Returns {capability_version_id, inserted, skipped, total_evidence}.
    """
    conn = connect()
    try:
        return queries.ingest_symbols(
            conn,
            capability_version_id=capability_version_id,
            source_revision_id=source_revision_id,
        )
    finally:
        conn.close()


@mcp.tool()
def analyze_local(
    root: str,
    max_files: int = 5000,
    include_items: bool = False,
) -> dict[str, Any]:
    """
    Run all extractors against local files without the DB pipeline.

    Scans a directory, runs every registered extractor, and returns a
    structured report of all evidence found. No database, no connectors,
    no revisions needed.

    Args:
      root: directory path to scan.
      max_files: stop collecting after this many files (default 5000).
      include_items: if True, include individual evidence dicts in result
        (can be large). Default False returns only aggregate counts.

    Returns {root, files_scanned, files_skipped, total_evidence,
    by_extractor, by_type, by_language, items[], errors[]}.
    """
    from analysis.analyze_local import analyze_local as _analyze
    result = _analyze(root=root, max_files=max_files, include_items=include_items)
    return result.to_dict()


@mcp.tool()
def list_extractors() -> list[str]:
    """
    List all available evidence extractors.

    Returns the names of all auto-discovered extractors in the analysis
    pipeline. Each name corresponds to an extractor that runs during
    analysis and produces evidence items.

    Returns a sorted list of extractor names.
    """
    return _extractor_names()


@mcp.tool()
def verification_status(
    capability_id: str,
) -> Optional[dict[str, Any]]:
    """
    Get the verification status of a capability's head version.

    Returns the latest verification runs with their assertions
    (install, build, import, smoke, contract checks). Shows whether
    the capability has been verified to install and run correctly.

    Args:
      capability_id: UUID of the capability to check.

    Returns {capability_id, normalized_key, version_id, display_version,
    verified, runs[]} where each run has {id, result, duration_ms,
    reproducibility_hash, assertions[]}. verified is True only when
    the latest run passed all assertions.
    """
    conn = connect()
    try:
        return queries.verification_status(
            conn,
            capability_id=capability_id,
        )
    finally:
        conn.close()


@mcp.tool()
def experience_summary(
    project_id: str,
    capability_id: str,
) -> Optional[dict[str, Any]]:
    """
    Get the experience summary for a capability within a project.

    Shows how well a component has worked in practice: active uses,
    failure count, experience score, and a recommendation (continue,
    monitor, or consider_replacement).

    Args:
      project_id: UUID of the project.
      capability_id: UUID of the capability.

    Returns {capability_id, normalized_key, project_id, active_uses,
    total_failures, unresolved_failures, experience_score, recommendation,
    uses[], failures[]}. recommendation is one of 'continue', 'monitor',
    'consider_replacement', or 'not_used'.
    """
    conn = connect()
    try:
        return queries.experience_summary(
            conn,
            project_id=project_id,
            capability_id=capability_id,
        )
    finally:
        conn.close()


@mcp.tool()
def find_replacement(
    project_id: str,
    capability_id: str,
    failure_kind: str = "other",
    severity: str = "error",
    summary: str = "",
    limit: int = 10,
) -> Optional[dict[str, Any]]:
    """
    Diagnose a component failure and find replacement candidates.

    When a component fails in a project, this tool analyzes the failure,
    determines urgency, and searches for alternatives — excluding the
    failed component and ranking by registry score + project experience.

    Args:
      project_id: UUID of the project where the failure occurred.
      capability_id: UUID of the failed capability.
      failure_kind: type of failure — 'install_failure', 'import_failure',
        'runtime_error', 'security_vulnerability', 'api_breaking_change',
        'dependency_conflict', 'build_failure', 'test_failure',
        'performance_degradation', 'other'.
      severity: 'warning', 'error', or 'critical'.
      summary: free-text description of the failure.
      limit: max replacement candidates to return (default 10).

    Returns {failed_capability_id, normalized_key, diagnosis, recommendation,
    candidates[]}. recommendation is one of 'replace_immediately',
    'replace_soon', 'monitor_and_plan', or 'no_alternatives_found'.
    """
    conn = connect()
    try:
        return queries.find_replacement(
            conn,
            project_id=project_id,
            capability_id=capability_id,
            failure_kind=failure_kind,
            severity=severity,
            summary=summary,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_detail(capability_id: str) -> Optional[dict[str, Any]]:
    """
    Full record for one capability.

    Args:
      capability_id: UUID of the capability row.

    Returns {id, normalized_key, display_name, ecosystem, capability_kind,
    component_kind, runtime, cost_tier, license_spdx, first_seen_at,
    head_version, interfaces, dependencies, scorecard} or None if the id
    does not exist. Every interface and dependency carries an
    evidence_item_id so the caller can trace back to the byte range
    that established it.
    """
    conn = connect()
    try:
        return queries.capability_detail(conn, capability_id=capability_id)
    finally:
        conn.close()


@mcp.tool()
def suggest_pipeline(
    intent: str,
    project_id: Optional[str] = None,
    max_stages: int = 5,
) -> dict[str, Any]:
    """
    Decompose an intent into an ordered pipeline of stages and propose
    a component pick per stage from the CIP registry.

    Args:
      intent: plain-English description of what the pipeline must do.
      project_id: optional UUID — when set, candidate picks are
        pre-filtered by that project's project_constraint set.
      max_stages: cap on stages (default 5, hard max 12).

    Returns {intent, project_id, stages, edges, notes}:
      stages[]: {index, name, purpose, role, preferred_component_kind,
                 search_terms, candidates[], pick}
      edges[]:  adjacent-stage compatibility verdicts
      notes[]:  strings flagging gaps (no candidates in registry, etc.)

    The proposal is READ-ONLY — no rows are written. The human is the
    designer; this is a starting point to react to.
    """
    from scripts.compose.suggest_pipeline import (
        suggest_pipeline as _suggest, MAX_STAGES,
    )
    capped = max_stages if max_stages <= MAX_STAGES else MAX_STAGES
    proposal = _suggest(intent=intent, project_id=project_id, max_stages=capped)
    return {
        "intent": proposal.intent,
        "project_id": proposal.project_id,
        "stages": proposal.stages,
        "edges": proposal.edges,
        "notes": proposal.notes,
    }


@mcp.tool()
def scaffold_pipeline(
    proposal: dict[str, Any],
    out_dir: str,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Turn a pipeline proposal (from suggest_pipeline, or hand-authored)
    into a real directory of files: pipeline.yaml, README.md, .env.example,
    and per-stage folders with README/TODO documentation. No LLM, no DB.

    Args:
      proposal: dict with keys {intent, project_id, stages[], edges[], notes[]}.
        Typically the return value of suggest_pipeline. Optional 'name' key
        may be embedded; overridden by the `name` arg if given.
      out_dir: directory to write into. Created if missing. Existing files
        are overwritten silently — meant for a fresh directory.
      name: optional override for the pipeline name. If neither `name` nor
        proposal['name'] is set, uses 'unnamed-pipeline'.

    Returns {out_dir, files_written[], env_vars_detected[]}.

    The scaffold is deliberately skeletal — it doesn't write runnable code.
    The wiring between stages is the operator's design call. Each stage
    folder has a TODO.md.
    """
    from pathlib import Path
    from scripts.compose.scaffold_pipeline import scaffold
    if name:
        proposal = {**proposal, "name": name}
    return scaffold(dict(proposal), Path(out_dir))


@mcp.tool()
def import_requirements(
    project_id: str,
    yaml_text: str,
) -> Optional[dict[str, Any]]:
    """
    Import requirements from a YAML spec into a project.

    Parses the YAML, creates or updates project_requirement rows and
    their requirement_constraint children. Existing requirements with
    the same slug are updated (constraints replaced).

    Args:
      project_id: UUID of the project to import into.
      yaml_text: YAML string with the requirements spec. Format:
        requirements:
          - slug: http-client
            description: "Need an HTTP client"
            constraints:
              - kind: required_interface
                name: get
              - kind: license_allowlist
                spdx_ids: [MIT, Apache-2.0]

    Returns {project_id, created, updated, total, requirements[]}
    or None if project_id is malformed/missing.
    """
    conn = connect()
    try:
        return queries.import_requirements(
            conn,
            project_id=project_id,
            yaml_text=yaml_text,
        )
    finally:
        conn.close()


@mcp.tool()
def requirement_coverage(
    project_id: str,
) -> Optional[dict[str, Any]]:
    """
    Requirement coverage report for a project.

    Shows each requirement and whether it has: a recommendation,
    a verification run on the recommended version, and experience
    data. Provides aggregate coverage percentage.

    Args:
      project_id: UUID of the project.

    Returns {project_id, total_requirements, covered, verified,
    with_experience, coverage_pct, requirements[]} where each
    requirement has {slug, description, constraint_count,
    has_recommendation, verdict, has_verification,
    verification_result, has_experience, experience_score}.
    """
    conn = connect()
    try:
        return queries.requirement_coverage(
            conn,
            project_id=project_id,
        )
    finally:
        conn.close()


@mcp.tool()
def inspection_history(
    capability_id: str,
    extractor_name: Optional[str] = None,
    limit: int = 20,
) -> Optional[dict[str, Any]]:
    """
    Inspection run history for a capability's head version.

    Shows which extractors have run, when, with what parser version,
    how many evidence items they produced, and whether any failed.
    Highlights extractors that have never run (missing coverage).

    Args:
      capability_id: UUID of the capability.
      extractor_name: optional filter — 'manifests', 'licenses',
        'interfaces', 'symbols', 'secrets', 'tests'.
      limit: max runs to return (default 20).

    Returns {capability_id, normalized_key, version_id, runs[],
    extractors_covered[], extractors_missing[]}. Each run has
    {id, extractor_name, extractor_version, source_revision,
    started_at, finished_at, status, evidence_count, error_detail}.
    """
    conn = connect()
    try:
        return queries.inspection_history(
            conn,
            capability_id=capability_id,
            extractor_name=extractor_name,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def authorize_build(
    recommendation_id: str,
    authorized_by: str,
    reason: str,
) -> Optional[dict[str, Any]]:
    """
    Authorize code generation for a BUILD recommendation.

    The search-before-build gate requires explicit authorization before
    any code can be generated. Refuses if the recommendation doesn't
    exist, isn't a BUILD verdict, or lacks search evidence.

    Args:
      recommendation_id: UUID of the BUILD recommendation.
      authorized_by: who is authorizing (actor name).
      reason: why build is authorized (audit trail).

    Returns {build_authorization_id, recommendation_id, authorized_by,
    status} or {error: "..."} on refusal, or None on bad id.
    """
    conn = connect()
    try:
        return queries.authorize_build(
            conn,
            recommendation_id=recommendation_id,
            authorized_by=authorized_by,
            reason=reason,
        )
    finally:
        conn.close()


@mcp.tool()
def build_gate_check(
    project_requirement_id: str,
) -> Optional[dict[str, Any]]:
    """
    Check whether code generation is allowed for a requirement.

    Returns whether a BUILD recommendation exists and has been
    authorized. Code generators must call this before proceeding.

    Args:
      project_requirement_id: UUID of the project_requirement row.

    Returns {project_requirement_id, allowed, reason,
    recommendation_id, build_authorization_id} or None on bad id.
    """
    conn = connect()
    try:
        return queries.build_gate_check(
            conn,
            project_requirement_id=project_requirement_id,
        )
    finally:
        conn.close()


@mcp.tool()
def build_coverage(
    project_id: str,
) -> Optional[dict[str, Any]]:
    """
    Build authorization coverage report for a project.

    Shows each requirement's recommendation verdict and whether
    BUILD verdicts have been authorized for code generation.

    Args:
      project_id: UUID of the project.

    Returns {project_id, total_requirements, build_verdicts, authorized,
    authorization_pct, requirements[]} where each requirement has
    {slug, verdict, is_build, has_authorization}.
    """
    conn = connect()
    try:
        return queries.build_coverage(
            conn,
            project_id=project_id,
        )
    finally:
        conn.close()


@mcp.tool()
def create_adapter_spec(
    source_id: str,
    target_id: str,
    adapter_hint: str = "",
    io_transform: Optional[dict] = None,
) -> Optional[dict[str, Any]]:
    """
    Create an adapter specification between two capabilities.

    Auto-classifies the bridge type from runtimes and generates
    skeleton code (adapter + contract tests) for the bridge.

    Args:
      source_id: UUID of the upstream/producing capability.
      target_id: UUID of the downstream/consuming capability.
      adapter_hint: optional hint string (e.g. from compatibility check).
      io_transform: optional dict describing I/O type mapping.

    Returns {id, source_id, target_id, bridge_kind, status, description,
    skeleton_code, test_code, ...} or None if ids are bad or pair exists.

    bridge_kind is one of: subprocess, http, mcp_client, type_transform,
    pip_install, skill_invoke, generic.
    """
    conn = connect()
    try:
        return queries.create_adapter_spec(
            conn,
            source_id=source_id,
            target_id=target_id,
            adapter_hint=adapter_hint,
            io_transform=io_transform,
        )
    finally:
        conn.close()


@mcp.tool()
def get_adapter_spec(
    source_id: str,
    target_id: str,
) -> Optional[dict[str, Any]]:
    """
    Look up an existing adapter specification by source+target pair.

    Args:
      source_id: UUID of the upstream capability.
      target_id: UUID of the downstream capability.

    Returns the full adapter_spec record including skeleton_code and
    test_code, or None if not found.
    """
    conn = connect()
    try:
        return queries.get_adapter_spec(
            conn, source_id=source_id, target_id=target_id,
        )
    finally:
        conn.close()


@mcp.tool()
def list_adapter_specs(
    capability_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    List adapter specifications, optionally filtered.

    Args:
      capability_id: optional UUID — show only specs involving this
        capability (as source or target).
      status: optional filter — 'draft', 'generated', 'reviewed', 'tested'.
      limit: max rows (1-200, default 50).

    Returns rows with {id, source_id, target_id, bridge_kind,
    source_runtime, target_runtime, status, source_key, target_key}.
    """
    conn = connect()
    try:
        return queries.list_adapter_specs(
            conn,
            capability_id=capability_id,
            status=status,
            limit=limit,
        )
    finally:
        conn.close()


def main() -> None:
    """Entry point for the `cip-mcp` console script. Runs stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
