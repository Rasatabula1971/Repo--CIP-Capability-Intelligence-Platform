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

import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from analysis.extractors import extractor_names as _extractor_names
from db.connection import connect
from mcp_server import queries
from mcp_server import pdr_queries
from mcp_server import pdr_store
from mcp_server import eval_queries

log = logging.getLogger(__name__)


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
    # This tool writes files whose content the caller controls. Log each
    # one, so that if it is later ingested as a PDR the human is told a
    # CIP tool wrote it. Best-effort: the scaffold itself needs no database.
    #
    # In a `finally`: the scaffold writes README.md before it reads the
    # edges, so a proposal that makes it raise there leaves the file on
    # disk. Logging only after a clean return left that file unlabelled.
    # When it raised there is no list of what was written, so everything
    # the scaffold could have written in out_dir is logged instead.
    result = None
    try:
        result = scaffold(dict(proposal), Path(out_dir))
    finally:
        written = (result["files_written"] if result
                   else _scaffold_files_on_disk(Path(out_dir)))
        try:
            conn = connect()
            try:
                pdr_store.record_files_written_by_tool(
                    conn, "scaffold_pipeline", str(Path(out_dir)), written)
                conn.commit()
            finally:
                conn.close()
        except Exception:                         # noqa: BLE001
            log.warning("scaffold_pipeline: could not log written files", exc_info=True)
    return result


def _scaffold_files_on_disk(out_dir) -> list[str]:
    """Every path scaffold_pipeline can write under out_dir that exists."""
    found = [name for name in ("pipeline.yaml", ".env.example", "README.md")
             if (out_dir / name).is_file()]
    try:
        for name in ("README.md", "TODO.md"):
            found += sorted(str(p.relative_to(out_dir))
                            for p in out_dir.glob(f"stages/*/{name}") if p.is_file())
    except OSError:
        pass
    return found


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
# PDR-to-Build workflow tools (Implementation Spec v1.1)
#
# Order of use: pdr_status → ingest_pdr → record_requirements →
# record_capability_needs → request_requirements_confirmation → (human)
# → search_for_requirement → record_decision → pdr_approval_brief →
# lock_architecture → (human) → lock_architecture(approval_request_id) →
# record_build_progress / check_against_lock → pdr_coverage.
#
# There is deliberately no tool that records a human approval. Approvals
# are given in a terminal with `cip-composer approvals`.
# -----------------------------------------------------------------------

@mcp.tool()
def pdr_status(
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Where a project is in the PDR-to-Build workflow, and what is allowed next.

    CALL THIS FIRST in every session. A build spans many sessions and a
    fresh context has none of the ids. READ-ONLY.

    Args:
      project_id: UUID of the project, or
      project_name: the name it was ingested under.

    Returns {project_id, phase, next_actions[], pdr, requirements{hash,
    confirmation, blockers[], items[]}, needs[], pending_approvals[],
    obsolete_requests, approved_lock_request{approval_request_id, ...} |
    null (an approved lock waiting to be written: pass its id to
    lock_architecture), lock{version, usable, stale_reasons[],
    drifted_needs[], decisions[], approved_providers[],
    cost_commitments[], project_decisions[], unresolved_risks[]} | null,
    tasks[], progress, compliance} or None if the project is not found.

    A task stored as verified reads back as implemented here if its
    evidence no longer covers every acceptance criterion.
    """
    conn = connect()
    try:
        return pdr_queries.pdr_status(
            conn, project_id=project_id, project_name=project_name,
        )
    finally:
        conn.close()


@mcp.tool()
def ingest_pdr(
    project_name: str,
    pdr_title: str,
    pdr_path: Optional[str] = None,
    pdr_text: Optional[str] = None,
    project_id: Optional[str] = None,
    stack: Optional[str] = None,
    policy_profile: Optional[str] = None,
) -> dict[str, Any]:
    """
    Register a project and store the PDR as the provenance root.

    Use pdr_path. The server reads the file itself, records the file's
    SHA-256, and the human is shown the file name and hash when asked to
    confirm the requirements — so they can tell which document they are
    approving. Text passed as pdr_text has no file behind it and is shown
    to the human as "supplied inline, not read from a file".

    pdr_path must be ABSOLUTE (this server does not run in your project
    folder) and a text document: .md, .markdown, .txt, .rst or .text.

    Ingesting a different text for an existing project makes it the
    current PDR. Requirements whose quotes still appear are carried
    forward; the rest are reported as orphaned and must be re-recorded or
    retired. Requirements then need confirming again and an existing lock
    goes stale. Ingesting an earlier text again makes that one current.

    Args:
      project_name: name for the project (created if new).
      pdr_title: title of the PDR document (required).
      pdr_path: absolute path to the PDR file, readable by this server. OR
      pdr_text: the full text (short documents only).
      project_id: optional UUID — use an existing project.
      stack: optional tech stack hint (stored in project metadata).
      policy_profile: optional policy profile name.

    Returns {project_id, requirement_source_id, content_hash, was_duplicate,
    is_current, supersedes_id, carried_forward[]?, orphaned[]?} or {error}.
    """
    conn = connect()
    try:
        return pdr_queries.ingest_pdr(
            conn, project_name=project_name, pdr_title=pdr_title,
            pdr_text=pdr_text, pdr_path=pdr_path, project_id=project_id,
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
    Record requirements extracted from the PDR.

    Each requirement must carry a verbatim `quote` from the PDR. CIP finds
    the quote in the stored text and computes the span itself; a quote
    that is not in the PDR is refused. Do not send character offsets.
    Quote the sentence that states the requirement: at most 600
    characters, and not running across a heading into another section.

    These are what the PDR asks for, not what gets searched. Reusable
    capabilities go in record_capability_needs.

    Args:
      project_id: UUID of the project.
      requirement_source_id: UUID from ingest_pdr (the current PDR).
      requirements: list of {
        text, quote,
        slug?, kind? ('functional' | 'non_functional' | 'constraint'),
        priority? ('must' | 'should' | 'could' | 'wont'),
        acceptance_criteria: [str, ...]  (each one testable),
        disposition? ('bespoke' | 'not_applicable' | 'deferred'),
        disposition_reason? (required with a disposition)
      }.
      Leave disposition out for anything a capability need will satisfy.

    Returns {requirements: [{index, req_id, slug, valid, errors[], span}],
    requirements_hash, span_coverage{unmapped_sections[],
    out_of_scope_only_sections[], lightly_quoted_sections[]}}.

    Always give a `slug`. Re-recording with an explicit slug updates that
    requirement. Without one a slug is derived from the text, and if it
    already exists the item is refused: CIP will not guess that an update
    was meant. A slug may appear once per call. Changing a requirement's
    text, quote or kind discards the verification evidence recorded
    against it. Any change re-opens the requirements confirmation.
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
def record_capability_needs(
    project_id: str,
    needs: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """
    Record the reusable capabilities the build needs, and link each to the
    PDR requirements it serves.

    A capability need (auth, job queue, PDF render) is the unit CIP
    searches and decides. Derive them from the architecture sketch: one
    need can serve several requirements, and one requirement can need
    several capabilities. Linking a need sets the requirement's
    disposition to via_needs.

    Give constraints wherever you can. A need with no constraint cannot be
    fit-scored, so its decision is recorded as LLM judgment, not CIP's.

    search_terms and problem_types are part of what the human confirms:
    they decide what the registry is asked. Changing either re-opens the
    requirements confirmation and voids earlier searches. A search term
    may not mix alphabets inside a word (a Cyrillic 'o' in 'oauth').
    Always give a `slug`; a derived slug that already exists is refused.

    Args:
      project_id: UUID of the project.
      needs: list of {
        description, slug?,
        search_terms: [1-8 short keyword phrases, e.g. 'oauth', 'pdf render'],
        problem_types?: [tags used to recall banked evaluations, e.g. 'oauth-flow'],
        satisfies: [requirement slugs or ids],
        constraints?: [
          {kind: 'required_interface', name, signature_contains?} |
          {kind: 'license_allowlist', spdx_ids: [...]} |
          {kind: 'forbidden_dependency', ecosystem, name}
        ]
      }.

    Returns {needs: [{index, need_id, slug, valid, errors[], warnings[],
    satisfies[]}], requirements_hash, blockers[]} or None.
    """
    conn = connect()
    try:
        return pdr_queries.record_capability_needs(
            conn, project_id=project_id, needs=needs,
        )
    finally:
        conn.close()


@mcp.tool()
def retire_requirement(
    req_id: str,
    reason: str,
) -> Optional[dict[str, Any]]:
    """
    Take a requirement or a capability need out of scope, on the record.

    Use this when a need turns out to be unwanted — REJECT is not a valid
    decision for a need. The row is kept; re-recording the slug restores it.

    Args:
      req_id: UUID of the requirement or need.
      reason: why it is being retired.

    Returns {req_id, slug, kind, status, blockers[]} or None.
    """
    conn = connect()
    try:
        return pdr_queries.retire_requirement(conn, req_id=req_id, reason=reason)
    finally:
        conn.close()


@mcp.tool()
def request_requirements_confirmation(
    project_id: str,
) -> Optional[dict[str, Any]]:
    """
    HUMAN GATE 1. Put the requirement set in front of the human.

    Opens an approval request for the current requirements and needs, and
    reports its state. search_for_requirement and record_decision refuse
    until a human approves this exact content; any later edit re-opens
    the gate, and so does returning to content approved earlier. Show the
    returned summary_markdown to the human, then STOP and wait. You
    cannot approve it and must not try to.

    Args:
      project_id: UUID of the project.

    On a second or later confirmation the summary opens with what changed
    since the scope the human last confirmed, and lists everything
    retired.

    Returns {status: 'blocked' | 'awaiting_approval' | 'approved' |
    'rejected', blockers[]?, approval_request_id?, requirements_hash,
    span_coverage, changes_since_last_confirmation[], summary_markdown?,
    how_to_approve?} or None.
    """
    conn = connect()
    try:
        return pdr_queries.request_requirements_confirmation(
            conn, project_id=project_id,
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
    Search the registry and the evaluation store for one capability need.

    Call it with only req_id first. That is the mandatory sweep: every one
    of the need's confirmed search_terms, unfiltered, taking up to 100
    hits per term whatever `limit` says. record_decision requires it. A
    search_query or a filter gives extra evidence but never replaces the
    sweep. An empty-string filter is treated as no filter.

    If a term returns 100 hits the result says `truncated: true`: there
    may be matches the search never looked at, and CIP will then not
    conclude on its own that nothing fits. Matches CIP could not evaluate
    (no source to pin, or excluded by the project's policy) are listed in
    `unevaluable`, and are not the same as having found nothing.

    Every call is logged, including one that finds nothing. An empty
    result is reported as no registry coverage — a fact about what CIP has
    ingested, not proof nothing exists.

    Each candidate says whether CIP holds the evidence its constraints
    need (`evidence`: 'sufficient' | 'insufficient', with
    `missing_evidence`: license / interfaces / dependencies / score),
    read from the revision that would be pinned. A candidate known only
    from registry metadata scores as a gap because nothing was read, not
    because it failed. `confirmed_blocking_gaps` are the gaps that do
    rest on something CIP read.

    Args:
      req_id: UUID of the capability need (need_id from record_capability_needs).
      search_query: optional extra query text.
      ecosystem / capability_kind / component_kind / runtime / cost_tier:
        optional filters.
      limit: max candidates per query term (default 20).

    Returns {req_id, slug, search_id, queries[], counts_as_mandatory_sweep,
    registry_size, truncated, policy_excluded_count,
    candidates[{capability_version_id, normalized_key, fit_score,
    blocking_gap_count, confirmed_blocking_gaps[], intrinsic_score,
    license, license_source, cost_tier, evidence, missing_evidence[]}],
    unevaluable[], evaluation_matches[{asset_slug, verdict, banked,
    evidence_depth, selectable}], coverage_outcome} or {refused, reason}
    or None.
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
    verdict: Optional[str] = None,
    chosen_capability_version_id: Optional[str] = None,
    rationale: str = "",
) -> Optional[dict[str, Any]]:
    """
    Record the reuse/build decision for one capability need.

    CIP computes the verdict first, from its rules, over the candidates
    this need's searches evaluated. `verdict` is YOUR PROPOSAL. Omit it to
    take CIP's.

      - you agree or omit it        → decided_by 'cip_rules'
      - you disagree                → 'llm_override' (rationale required)
      - CIP cannot compute a verdict → 'llm_judgment' (verdict and
        rationale required). That is the case when the need has no
        machine-checkable constraint; when CIP lacks the evidence for
        the candidate it would choose; or, for BUILD, when it lacks the
        evidence for ANY candidate or the sweep was truncated. "Nothing
        fits" is a claim about all of them.

    The sweep must describe the registry as it is: if the registry has
    changed since the sweep, this call is refused until you sweep again.

    Overrides and LLM judgments are listed for the human at lock time.
    Only candidates returned by search_for_requirement can be chosen.
    REJECT is not valid here: if nothing fits, propose BUILD; if the need
    is unwanted, retire_requirement.

    Args:
      req_id: UUID of the capability need.
      verdict: optional proposal — ADOPT, ADAPT, WRAP, REFERENCE or BUILD.
      chosen_capability_version_id: required with ADOPT/ADAPT/WRAP/REFERENCE.
      rationale: why; required unless you take CIP's verdict.

    Returns {decision_record_id, recommendation_id, verdict, decided_by,
    computed_verdict, computed_rule, coverage_outcome, flags[],
    listed_for_human} or {refused, reason} or None.
    """
    conn = connect()
    try:
        return pdr_queries.record_decision(
            conn, req_id=req_id, verdict=verdict,
            chosen_capability_version_id=chosen_capability_version_id,
            rationale=rationale,
        )
    finally:
        conn.close()


@mcp.tool()
def pdr_approval_brief(
    project_id: str,
    architecture_doc_path: Optional[str] = None,
    architecture_doc_text: Optional[str] = None,
    approved_providers: Optional[list[dict[str, Any]]] = None,
    cost_commitments: Optional[list[dict[str, Any]]] = None,
    project_decisions: Optional[list[dict[str, Any]]] = None,
    unresolved_risks: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """
    Preview exactly what lock_architecture would freeze. READ-ONLY.

    Pass the same inputs you will pass to lock_architecture: the brief and
    the lock are computed by one function. Items needing the human's
    attention come from structured facts (who decided, license, cost
    tier, provider category), not from keywords.

    Args: as lock_architecture.

    Returns {brief_markdown, decisions[], human_approval_items[{code,
    subject, detail}], unresolved[] (blockers), changes_from_active_lock[],
    payload_hash, approval_state, ready_to_lock} or None.
    """
    conn = connect()
    try:
        return pdr_queries.build_approval_brief(
            conn, project_id=project_id,
            architecture_doc_path=architecture_doc_path,
            architecture_doc_text=architecture_doc_text,
            approved_providers=approved_providers,
            cost_commitments=cost_commitments,
            project_decisions=project_decisions,
            unresolved_risks=unresolved_risks,
        )
    finally:
        conn.close()


@mcp.tool()
def lock_architecture(
    project_id: str,
    architecture_doc_path: Optional[str] = None,
    architecture_doc_text: Optional[str] = None,
    approved_providers: Optional[list[dict[str, Any]]] = None,
    cost_commitments: Optional[list[dict[str, Any]]] = None,
    project_decisions: Optional[list[dict[str, Any]]] = None,
    unresolved_risks: Optional[list[str]] = None,
    approval_request_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    HUMAN GATE 2. Freeze the approved decisions into a new lock version.

    Call it twice, with the human in between:
      1st call → the lock inputs. Returns status 'awaiting_approval' and
                 an approval_request_id. Show summary_markdown to the
                 human, then STOP and wait.
      (human approves in a terminal)
      2nd call → project_id and approval_request_id ONLY. Replays exactly
                 what was approved and writes the lock. Works from a new
                 session: pdr_status returns the id as
                 approved_lock_request.

    You do not name the approver or write the approval note; both come
    from the person who approves in the terminal. Changing any input
    changes the payload and needs a fresh approval. An approval is tied
    to the lock version it replaces and writes one lock, once. Only the
    lock request asked for most recently can be approved: asking for a
    different payload retires the earlier request. A relock shows its
    diff from the active lock — that is the change request.

    The lock also authorises code generation (Step 13) for every BUILD
    need, in the approver's name.

    Args:
      project_id: UUID of the project.
      architecture_doc_path: ABSOLUTE path to the architecture document
        (.md, .markdown, .txt, .rst, .text; hashed into the lock), OR
        architecture_doc_text.
      approved_providers: [{name, category ('ai'|'auth'|'payment'|'hosting'|
        'database'|'storage'|'email'|'messaging'|'analytics'|'other'),
        purpose, cost ('free'|'free_tier'|'usage'|'recurring'|'one_off'),
        data_leaves_infra: bool}] — every external service the build uses.
      cost_commitments: [{item, kind, amount, currency, period}].
      project_decisions: [{key, category, choice, rationale}] — choices
        that belong to no single requirement (database, hosting, auth model).
      unresolved_risks: [str].
      approval_request_id: second call only — the approved request.

    Returns {status: 'awaiting_approval', approval_request_id,
    summary_markdown, human_approval_items[], changes_from_active_lock[],
    how_to_approve, then} or {architecture_lock_id, version, payload_hash,
    approved_by, build_authorizations, unchanged?} or {refused, reason,
    blockers[]} or {error} or None.
    """
    conn = connect()
    try:
        return pdr_queries.lock_architecture(
            conn, project_id=project_id,
            architecture_doc_path=architecture_doc_path,
            architecture_doc_text=architecture_doc_text,
            approved_providers=approved_providers,
            cost_commitments=cost_commitments,
            project_decisions=project_decisions,
            unresolved_risks=unresolved_risks,
            approval_request_id=approval_request_id,
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

    Call this BEFORE: adding or swapping a dependency, using a new
    external provider, changing a locked capability, changing a locked
    project decision (database, hosting, auth model...), or editing a
    file outside the task's permitted_paths. Every call is logged.

    The input must be structured. A description alone returns 'blocked',
    and a key this tool does not know is escalated, never ignored.

    Args:
      project_id: UUID of the project.
      proposed_change: {
        description,
        build_task_id?, changed_paths?: [repo-relative paths],
        capability_changes?: [{need: slug|id, capability_version_id?, verdict?}],
        provider_changes?: [{name, action: 'add'|'use'|'remove'|'change'}],
        dependency_adds?: [{ecosystem, name}],
        project_decision_changes?: [{key, choice}]
      }

    Returns {verdict: 'allowed' | 'needs_approval' | 'blocked', reasons[],
    cited[] (the locked items each reason rests on), reminders[] (the
    task's prohibited_changes), lock_version, next} or None.
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
    verification: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """
    Create a build task, or report progress and verification on one.

    CREATE (no build_task_id) — this is the build gate. Refused unless
    the lock is active and current, every linked requirement is in scope,
    every capability need it rests on is locked with an unchanged
    decision, and BUILD needs are authorised.
      project_id, title, objective,
      req_ids: slugs or ids. At least one PDR requirement (what the task
        delivers); add the capability need when the task integrates or
        builds one,
      permitted_paths: REQUIRED — repo-relative files/directories the
        task may touch. The first path segment must be literal: 'src/**'
        is fine, '**' or '*/x' is refused,
      prohibited_changes?: list of free-text reminders,
      reused_capability_version_id?: normally leave it out. CIP fills it
        from the lock for whichever ADOPT/ADAPT/WRAP capability the task
        rests on — including through a requirement that need serves —
        and returns it. If you pass one it must equal the locked version.

    UPDATE (build_task_id given):
      status: planned → in_progress → implemented → verified, or abandoned.
      target_commit: commit SHA in the target repo.
      verification: one object or a list of {
        criterion_id | (req_id + criterion_ordinal),
        outcome: 'pass' | 'fail' | 'not_run',
        commit_sha, test_ref, command, exit_code   (all required for a pass),
        output_digest?, evidence_ref?
      }
    'verified' requires a passing latest verification for every acceptance
    criterion of every linked requirement. It does not outlive its
    evidence: a later fail, a new criterion or a rewritten criterion
    moves the task back to implemented. Evidence counts only while the
    task that recorded it is current and not abandoned. Updates are
    refused while the lock is stale, and while a decision the task rests
    on has changed since the lock (you may still abandon a task). An
    abandoned task is closed.
    Verifications reported here are recorded as agent_reported; the human
    can run `cip-composer verify` to have CIP execute the test itself.
    commit_sha must be a hex git SHA.

    Returns {build_task_id, status, ...} or {error, refused?, reasons[]?}.
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
    How much of the PDR is built, and on what evidence. READ-ONLY.

    Reports its denominators: PDR sections with no requirement mapped;
    requirements in scope vs deferred; what the registry could answer and
    who decided; in-scope requirements decided → locked → tasked →
    implemented → verified, with verified split by evidence source
    (agent_reported vs cip_executed); and compliance signals such as
    tasks worked on with no check_against_lock on record.

    Args:
      project_id: UUID of the project.

    Returns {project_id, project_name, pdr, requirements, needs, progress,
    compliance, by_requirement[]} or None.
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

    Sends the PDR to the embedded FAIR router, which routes through
    free models (Gemini, Groq, OpenRouter free, Ollama) and returns
    requirement drafts in the shape record_requirements takes: slug, text,
    priority, kind, a verbatim quote, and a list of acceptance criteria.

    They are drafts from a free model. Writes nothing. Pass them to
    record_requirements, which locates each quote in the stored PDR and
    refuses any it cannot find, and read them first: a model can miss a
    requirement or invent one.

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

    Sends the capability need and its search candidates to FAIR for
    evaluation. Returns a suggested verdict (ADOPT/ADAPT/WRAP/REFERENCE/
    BUILD) with rationale.

    A suggestion from a free model. Writes nothing and decides nothing.
    Pass it to record_decision as your proposal: CIP computes its own
    verdict there, and a proposal that differs is recorded as an override
    or as LLM judgment and shown to the human at lock time.

    Args:
      req_id: UUID of the capability need.
      description: the need's description text.
      constraints: list of constraint dicts from the need.
      candidates: the `candidates` list from search_for_requirement, as
        returned (the evidence fields on each candidate are used).

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
    Check whether FAIR is installed and has at least one provider configured.

    Returns {available, client_id, providers[], error}. Each provider has
    {provider_id, status, access_class, models[]}. error explains why
    available is False (package missing, no provider keys, ...).
    """
    from integrations.fair_client import FairClient
    return FairClient().status()


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
    evidence_depth: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record an asset evaluation in the durable evaluation store.

    Upserts by asset_slug — re-evaluating the same asset overwrites the
    prior row. This is the proper home for repo-scout / co-work verdicts,
    replacing lossy markdown files. Save every evaluation in bucket 1
    (fits a project) or bucket 2 (banked); skip only pure junk.

    Args:
      asset_slug: unique slug, e.g. 'msitarzewski-agency-agents'.
      verdict: Adopt | Adapt | Wrap | Reference | Reject | Build.
      asset_type: repo | skill | agent | mcp_server | prompt | workflow.
      url: source URL.
      evidence_depth: REQUIRED. How far you actually looked:
        'code_read' (read the source), 'docs_only' (README/docs),
        'metadata_only' (stars, license, description).
      code_quality: solid | acceptable | fragile | untested. Only allowed
        with evidence_depth 'code_read'; omit it otherwise.
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
            evaluated_at=evaluated_at, evidence_depth=evidence_depth,
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
