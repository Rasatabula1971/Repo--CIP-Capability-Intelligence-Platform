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
from mcp_server import pdr_queries
from mcp_server import eval_queries


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


# -----------------------------------------------------------------------
# PDR-to-Build workflow tools (vNext)
# -----------------------------------------------------------------------

@mcp.tool()
def ingest_pdr(
    project_name: str,
    pdr_title: str,
    pdr_text: str,
    project_id: Optional[str] = None,
    stack: Optional[str] = None,
    policy_profile: Optional[str] = None,
) -> dict[str, Any]:
    """
    Register a project and store the PDR as the provenance root.

    Creates the project if it doesn't exist. Re-ingesting identical text
    returns the same content_hash without duplicating the source row.

    Args:
      project_name: name for the project (used for create-or-lookup).
      pdr_title: title of the PDR document.
      pdr_text: full text of the PDR.
      project_id: optional UUID — use existing project instead of creating.
      stack: optional tech stack hint (stored in project metadata).
      policy_profile: optional policy profile name.

    Returns {project_id, requirement_source_id, content_hash, was_duplicate}.
    """
    conn = connect()
    try:
        return pdr_queries.ingest_pdr(
            conn, project_name=project_name, pdr_title=pdr_title,
            pdr_text=pdr_text, project_id=project_id,
            stack=stack, policy_profile=policy_profile,
        )
    finally:
        conn.close()


@mcp.tool()
def record_requirements(
    project_id: str,
    requirement_source_id: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Submit extracted requirements from a PDR for validation and storage.

    Each requirement must be atomic (one testable need), have non-empty
    acceptance_criteria, a valid priority, and a source_span linking it
    back to the PDR text.

    Args:
      project_id: UUID of the project.
      requirement_source_id: UUID from ingest_pdr.
      requirements: list of {text, priority, acceptance_criteria,
        source_span, slug?, constraints?[]}.
        priority: 'must' | 'should' | 'could' | 'wont'.
        source_span: {start, end, quote} pointing into the PDR.
        constraints: optional [{kind, ...}] for requirement_constraint rows.

    Returns {project_id, requirement_source_id, requirements: [{req_id,
    slug, valid, errors}]}.
    """
    conn = connect()
    try:
        return pdr_queries.record_requirements(
            conn, project_id=project_id,
            requirement_source_id=requirement_source_id,
            requirements=requirements,
        )
    finally:
        conn.close()


@mcp.tool()
def search_for_requirement(
    req_id: str,
    search_query: Optional[str] = None,
    ecosystem: Optional[str] = None,
    capability_kind: Optional[str] = None,
    component_kind: Optional[str] = None,
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    limit: int = 20,
) -> Optional[dict[str, Any]]:
    """
    Search the CIP registry and compute fit for one requirement.

    Searches the capability registry using search_query (defaults to the
    requirement description), evaluates fit against each candidate, and
    persists fit_evaluation rows. Call this before record_decision — a
    BUILD verdict is refused without a prior search.

    Args:
      req_id: UUID of the project_requirement row.
      search_query: text to search for (defaults to requirement description).
      ecosystem: filter by ecosystem (pypi, npm, source, ...).
      capability_kind: filter by semantic role (library, cli, service, ...).
      component_kind: filter by format (library, repo, agent, ...).
      runtime: filter by runtime (python_import, mcp_stdio, ...).
      cost_tier: filter by cost (free, free_tier, cheap_paid, paid).
      limit: max candidates to evaluate (default 20).

    Returns {req_id, slug, description, constraints, candidates[],
    candidate_count, search_query} or None if not found.
    """
    conn = connect()
    try:
        return pdr_queries.search_for_requirement(
            conn, req_id=req_id,
            search_query=search_query,
            ecosystem=ecosystem,
            capability_kind=capability_kind,
            component_kind=component_kind,
            runtime=runtime,
            cost_tier=cost_tier,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def record_decision(
    req_id: str,
    verdict: str,
    chosen_capability_version_id: Optional[str] = None,
    evidence_refs: Optional[list[str]] = None,
    rationale: str = "",
) -> Optional[dict[str, Any]]:
    """
    Record the reuse/build verdict for a requirement.

    Enforces search-before-build: BUILD is refused unless a search
    (fit_evaluation) exists for the requirement. Reuse verdicts require
    a pinned capability_version_id.

    Args:
      req_id: UUID of the project_requirement.
      verdict: one of ADOPT, ADAPT, WRAP, REFERENCE, REJECT, BUILD.
      chosen_capability_version_id: required for ADOPT/ADAPT/WRAP/REFERENCE.
      evidence_refs: optional list of evidence item UUIDs.
      rationale: free-text reason for the decision.

    Returns {recommendation_id, verdict} or {refused, reason} or None.
    """
    conn = connect()
    try:
        return pdr_queries.record_decision(
            conn, req_id=req_id, verdict=verdict,
            chosen_capability_version_id=chosen_capability_version_id,
            evidence_refs=evidence_refs, rationale=rationale,
        )
    finally:
        conn.close()


@mcp.tool()
def pdr_approval_brief(
    project_id: str,
) -> Optional[dict[str, Any]]:
    """
    Render the plain-language approval brief for a project.

    READ-ONLY — writes nothing. Shows every requirement's decision,
    flags items needing human approval (paid services, unusual licenses,
    vendor lock-in, security implications).

    Args:
      project_id: UUID of the project.

    Returns {brief_markdown, decisions[], human_approval_items[],
    unresolved[]} or None if project not found.
    """
    conn = connect()
    try:
        return pdr_queries.build_approval_brief(conn, project_id=project_id)
    finally:
        conn.close()


@mcp.tool()
def lock_architecture(
    project_id: str,
    approved_by: str,
    approval_note: Optional[str] = None,
    unresolved_risks: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """
    Freeze approved decisions into a versioned, append-only lock.

    Supersedes any existing active lock and creates a new version.
    All requirements must have a recorded decision before locking.

    Args:
      project_id: UUID of the project.
      approved_by: who is approving (actor name).
      approval_note: optional note for the audit trail.
      unresolved_risks: optional list of known risks to record.

    Returns {architecture_lock_id, version, source_lock_hash,
    decision_count} or {refused, reason} or None.
    """
    conn = connect()
    try:
        return pdr_queries.lock_architecture(
            conn, project_id=project_id, approved_by=approved_by,
            approval_note=approval_note, unresolved_risks=unresolved_risks,
        )
    finally:
        conn.close()


@mcp.tool()
def check_against_lock(
    project_id: str,
    proposed_change: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """
    Test a proposed change against the active architecture lock.

    READ-ONLY — writes nothing. Returns whether the change is allowed
    or needs approval.

    Args:
      project_id: UUID of the project.
      proposed_change: {req_ids?, provider?, capability_version_id?,
        description} describing the proposed change.

    Returns {verdict: 'allowed'|'needs_approval', reason} or None.
    """
    conn = connect()
    try:
        return pdr_queries.check_against_lock(
            conn, project_id=project_id, proposed_change=proposed_change,
        )
    finally:
        conn.close()


@mcp.tool()
def record_build_progress(
    project_id: Optional[str] = None,
    architecture_lock_id: Optional[str] = None,
    title: Optional[str] = None,
    objective: Optional[str] = None,
    req_ids: Optional[list[str]] = None,
    permitted_paths: Optional[list[str]] = None,
    prohibited_changes: Optional[list[str]] = None,
    reused_capability_version_id: Optional[str] = None,
    build_task_id: Optional[str] = None,
    status: Optional[str] = None,
    target_commit: Optional[str] = None,
    verification: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """
    Record coding tasks and link built modules/tests to requirements.

    Two modes:
    - Create: provide project_id, architecture_lock_id, title, objective,
      req_ids. Returns {build_task_id, status}.
    - Update: provide build_task_id with optional status, target_commit,
      verification. Returns {build_task_id, status}.

    Args:
      project_id: UUID (create mode).
      architecture_lock_id: UUID (create mode).
      title: task title (create mode).
      objective: what this task accomplishes (create mode).
      req_ids: list of requirement UUIDs this task satisfies (create mode).
      permitted_paths: optional file paths this task may touch.
      prohibited_changes: optional architectural changes forbidden.
      reused_capability_version_id: optional UUID for reuse tasks.
      build_task_id: UUID (update mode).
      status: planned|in_progress|implemented|verified|abandoned.
      target_commit: commit SHA in the target repo.
      verification: {req_id, outcome, evidence_ref} to record.

    Returns {build_task_id, status} or {error}.
    """
    conn = connect()
    try:
        return pdr_queries.record_build_progress(
            conn, project_id=project_id,
            architecture_lock_id=architecture_lock_id,
            title=title, objective=objective,
            req_ids=req_ids, permitted_paths=permitted_paths,
            prohibited_changes=prohibited_changes,
            reused_capability_version_id=reused_capability_version_id,
            build_task_id=build_task_id, status=status,
            target_commit=target_commit, verification=verification,
        )
    finally:
        conn.close()


@mcp.tool()
def pdr_coverage(
    project_id: str,
) -> Optional[dict[str, Any]]:
    """
    Report PDR implementation progress.

    READ-ONLY — writes nothing. Shows how much of the PDR is decided,
    tasked, implemented, and verified.

    Args:
      project_id: UUID of the project.

    Returns {project_id, project_name, total_requirements, decided,
    tasked, implemented, verified, by_requirement[]} or None.
    """
    conn = connect()
    try:
        return pdr_queries.coverage(conn, project_id=project_id)
    finally:
        conn.close()


# -----------------------------------------------------------------------
# FAIR integration tools
# -----------------------------------------------------------------------

@mcp.tool()
def fair_extract_requirements(
    pdr_text: str,
) -> dict[str, Any]:
    """
    Use FAIR (free AI inference) to extract requirements from PDR text.

    Sends the PDR to FAIR's /v1/solve endpoint which routes through
    free models (Groq, OpenRouter free, Ollama) to extract structured
    requirements ready for record_requirements.

    Args:
      pdr_text: the full PDR document text.

    Returns {requirements[], fair_request_id, provider_id, model_id,
    verification_state} or {error, requirements[]}.
    """
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import extract_requirements
    client = FairClient()
    return extract_requirements(client, pdr_text)


@mcp.tool()
def fair_suggest_verdict(
    req_id: str,
    description: str,
    constraints: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Use FAIR to suggest a reuse/build verdict for a requirement.

    Sends the requirement and search candidates to FAIR for
    evaluation. Returns a recommended verdict (ADOPT/ADAPT/WRAP/
    REFERENCE/REJECT/BUILD) with rationale.

    Args:
      req_id: UUID of the project_requirement.
      description: the requirement description text.
      constraints: list of constraint dicts from the requirement.
      candidates: list of candidate dicts from search_for_requirement.

    Returns {verdict, rationale, chosen_capability_version_id,
    fair_request_id, ...} or {error}.
    """
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import suggest_verdict
    client = FairClient()
    return suggest_verdict(
        client, req_id=req_id, description=description,
        constraints=constraints, candidates=candidates,
    )


@mcp.tool()
def fair_analyze_source(
    source_code: str,
    file_path: str = "<unknown>",
) -> dict[str, Any]:
    """
    Use FAIR to analyze source code and identify capabilities.

    Extracts capability metadata (name, kind, interfaces, dependencies)
    from source code using free AI inference.

    Args:
      source_code: the source code text to analyze.
      file_path: path to the file (for context in the prompt).

    Returns {capabilities[], fair_request_id, provider_id, model_id}
    or {error, capabilities[]}.
    """
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import analyze_source
    client = FairClient()
    return analyze_source(client, source_code, file_path)


@mcp.tool()
def fair_status() -> dict[str, Any]:
    """
    Check whether FAIR is reachable and configured.

    Returns {available, api_url, client_id}.
    """
    from integrations.fair_client import FairClient
    client = FairClient()
    return {
        "available": client.is_available(),
        "api_url": client.api_url,
        "client_id": client.client_id,
    }


# -----------------------------------------------------------------------
# Evaluation store tools (repo-scout / co-work asset evaluations)
# -----------------------------------------------------------------------

@mcp.tool()
def record_evaluation(
    asset_slug: str,
    verdict: str,
    asset_type: str = "repo",
    url: Optional[str] = None,
    code_quality: Optional[str] = None,
    banked: bool = False,
    summary: str = "",
    code_quality_notes: str = "",
    useful_parts: str = "",
    skip_notes: str = "",
    banked_references: Optional[list[dict]] = None,
    projects: Optional[list[str]] = None,
    problem_types: Optional[list[str]] = None,
    license_notes: str = "",
    cip_status: str = "not-ingested",
    evaluated_at: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record an asset evaluation in the durable evaluation store.

    Upserts by asset_slug — re-evaluating the same asset overwrites the
    prior row. This is the proper home for repo-scout / co-work verdicts,
    replacing lossy markdown files. Save every evaluation in bucket 1
    (fits a project) or bucket 2 (banked); skip only pure junk.

    Args:
      asset_slug: unique slug, e.g. 'msitarzewski-agency-agents'.
      verdict: Adopt | Adapt | Reference | Reject | Build.
      asset_type: repo | skill | agent | mcp_server | prompt | workflow.
      url: source URL.
      code_quality: solid | acceptable | fragile | untested.
      banked: true if worth keeping for the future regardless of project fit.
      summary: one-sentence what-it-is.
      code_quality_notes: what you found reading the source.
      useful_parts: specific files/patterns worth using.
      skip_notes: what to ignore.
      banked_references: list of {file, problem, why} dicts.
      projects: current projects it fits (or []).
      problem_types: problem-category tags — the primary recall key.
      license_notes: SPDX + implications.
      cip_status: ingested | not-ingested.
      evaluated_at: 'YYYY-MM-DD'.

    Returns the stored evaluation, or {"error": ...} on invalid input.
    """
    conn = connect()
    try:
        return eval_queries.record_evaluation(
            conn, asset_slug=asset_slug, verdict=verdict, asset_type=asset_type,
            url=url, code_quality=code_quality, banked=banked, summary=summary,
            code_quality_notes=code_quality_notes, useful_parts=useful_parts,
            skip_notes=skip_notes, banked_references=banked_references,
            projects=projects, problem_types=problem_types,
            license_notes=license_notes, cip_status=cip_status,
            evaluated_at=evaluated_at,
        )
    finally:
        conn.close()


@mcp.tool()
def search_evaluations(
    query: Optional[str] = None,
    problem_types: Optional[list[str]] = None,
    verdict: Optional[str] = None,
    asset_type: Optional[str] = None,
    banked: Optional[bool] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Search the evaluation store. Call this before building — it surfaces
    prior verdicts and banked references so nothing is re-evaluated.

    Args:
      query: substring across asset_slug, summary, useful_parts.
      problem_types: rows whose tags overlap ANY of these (primary recall).
      verdict: filter by verdict.
      asset_type: filter by asset type.
      banked: true to see only banked-for-future references.
      project: rows whose projects array contains this project.
      limit: max rows (1-500, default 50).

    Returns evaluation rows, newest first.
    """
    conn = connect()
    try:
        return eval_queries.search_evaluations(
            conn, query=query, problem_types=problem_types, verdict=verdict,
            asset_type=asset_type, banked=banked, project=project, limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def get_evaluation(asset_slug: str) -> Optional[dict[str, Any]]:
    """
    Fetch one evaluation by slug.

    Args:
      asset_slug: the unique slug used when it was recorded.

    Returns the full evaluation row, or None if not found.
    """
    conn = connect()
    try:
        return eval_queries.get_evaluation(conn, asset_slug=asset_slug)
    finally:
        conn.close()


def main() -> None:
    """Entry point for the `cip-mcp` console script. Runs stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
