"""
cip-composer — the CLI the user calls on from anywhere.

Four commands:

  cip-composer find <query>
      Keyword-search the registry. Optional filters mirror the MCP tool.

  cip-composer browse
      Browse without a keyword. Optional filters.

  cip-composer suggest <intent>
      Decompose an intent into a pipeline proposal. Writes JSON to stdout
      or to --out.

  cip-composer scaffold --proposal <path.json> --out-dir <dir>
      Turn a saved proposal into a real directory of files.

  cip-composer flow <intent> --out-dir <dir>
      End to end: suggest, save the proposal into <dir>/proposal.json,
      then scaffold into <dir>/.

  cip-composer info
      Print registry stats: counts per kind, per cost_tier, top-scored.

Every command reads DATABASE_URL / TEST_DATABASE_URL / GEMINI_API_KEY
from .env in the current working directory (same as the rest of CIP).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from analysis.extractors import extractor_names as _extractor_names
from db.connection import connect
from mcp_server import queries
from mcp_server import pdr_queries
from mcp_server import eval_queries
from scripts.compose.scaffold_pipeline import scaffold as _scaffold
from scripts.compose.suggest_pipeline import (
    suggest_pipeline as _suggest,
    MAX_STAGES,
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _print_row_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c) or "")) for r in rows))
              for c in columns}
    widths = {c: min(w, 60) for c, w in widths.items()}
    header = "  ".join(f"{c:<{widths[c]}}" for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        line = "  ".join(f"{str(r.get(c) or ''):<{widths[c]}}"[:widths[c]]
                          for c in columns)
        print(line)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_find(args) -> int:
    conn = connect()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    try:
        rows = queries.search_capabilities(
            conn, query=args.query,
            ecosystem=args.ecosystem,
            component_kind=args.kind,
            runtime=args.runtime,
            cost_tier=args.cost_tier,
            project_id=args.project_id,
            tags=tags,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
    else:
        _print_row_table(rows, ["normalized_key", "component_kind",
                                   "runtime", "cost_tier", "total_score",
                                   "license_spdx"])
    return 0


def cmd_browse(args) -> int:
    conn = connect()
    try:
        rows = queries.browse_components(
            conn, component_kind=args.kind,
            ecosystem=args.ecosystem,
            runtime=args.runtime,
            cost_tier=args.cost_tier,
            project_id=args.project_id,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
    else:
        _print_row_table(rows, ["normalized_key", "component_kind",
                                   "runtime", "cost_tier", "total_score"])
    return 0


def cmd_suggest(args) -> int:
    max_stages = min(args.max_stages, MAX_STAGES)
    proposal = _suggest(intent=args.intent, project_id=args.project_id,
                          max_stages=max_stages)
    payload = {
        "name": args.name or "unnamed-pipeline",
        "intent": proposal.intent,
        "project_id": proposal.project_id,
        "stages": proposal.stages,
        "edges": proposal.edges,
        "notes": proposal.notes,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2, default=str),
                                    encoding="utf-8")
        print(f"wrote proposal -> {args.out}")
    else:
        _print_json(payload)
    return 0


def cmd_scaffold(args) -> int:
    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    result = _scaffold(proposal, Path(args.out_dir))
    print(f"wrote {len(result['files_written'])} files to {result['out_dir']}")
    if result["env_vars_detected"]:
        print("env vars detected:", ", ".join(result["env_vars_detected"]))
    return 0


def cmd_flow(args) -> int:
    """suggest -> save proposal.json -> scaffold. All in one."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_stages = min(args.max_stages, MAX_STAGES)
    proposal = _suggest(intent=args.intent, project_id=args.project_id,
                          max_stages=max_stages)
    payload = {
        "name": args.name or out_dir.name or "unnamed-pipeline",
        "intent": proposal.intent,
        "project_id": proposal.project_id,
        "stages": proposal.stages,
        "edges": proposal.edges,
        "notes": proposal.notes,
    }
    proposal_path = out_dir / "proposal.json"
    proposal_path.write_text(json.dumps(payload, indent=2, default=str),
                              encoding="utf-8")
    result = _scaffold(payload, out_dir)
    print(f"proposal -> {proposal_path}")
    print(f"scaffold -> {result['out_dir']} ({len(result['files_written'])} files)")
    if result["env_vars_detected"]:
        print("env vars detected:", ", ".join(result["env_vars_detected"]))
    return 0


def cmd_search_symbols(args) -> int:
    conn = connect()
    try:
        rows = queries.search_symbols(
            conn, query=args.query,
            symbol_kind=args.symbol_kind,
            role=args.role,
            language=args.language,
            capability_id=args.capability_id,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
    else:
        _print_row_table(rows, ["qualified_name", "symbol_kind", "role",
                                "signature", "normalized_key"])
    return 0


def cmd_ingest_symbols(args) -> int:
    conn = connect()
    try:
        result = queries.ingest_symbols(
            conn,
            capability_version_id=args.capability_version_id,
            source_revision_id=getattr(args, "source_revision_id", None),
        )
    finally:
        conn.close()
    if result is None:
        print("Error: invalid ID")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Inserted: {result['inserted']}, "
              f"Skipped: {result['skipped']}, "
              f"Total evidence: {result['total_evidence']}")
    return 0


def cmd_analyze_local(args) -> int:
    from analysis.analyze_local import analyze_local
    result = analyze_local(
        root=args.root,
        max_files=args.max_files,
        include_items=args.include_items,
    )
    if result.errors and not result.files_scanned:
        print(f"Error: {result.errors[0].get('error', 'unknown')}")
        return 1
    if args.json:
        _print_json(result.to_dict())
    else:
        print(result.summary())
    return 0


def cmd_list_extractors(args) -> int:
    names = _extractor_names()
    if args.json:
        _print_json(names)
    else:
        for name in names:
            print(f"  {name}")
    return 0


def cmd_dep_fit(args) -> int:
    conn = connect()
    try:
        result = queries.dependency_fit(
            conn,
            capability_id=args.capability_id,
            project_id=args.project_id,
            profile_name=args.profile_name or "default",
        )
    finally:
        conn.close()
    if result is None:
        print("capability not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Capability: {result['normalized_key']}")
        print(f"Passed: {result['passed']}")
        if result["hard_failures"]:
            print("\nHard Failures:")
            for f in result["hard_failures"]:
                print(f"  [{f['fact_kind']}] {f['fact_key']}: {f['reason']} - {f['detail']}")
        if result["warnings"]:
            print("\nWarnings:")
            for w in result["warnings"]:
                print(f"  [{w['fact_kind']}] {w['fact_key']}: {w['reason']} - {w['detail']}")
        if result["package_deps"]:
            print(f"\nPackage dependencies ({len(result['package_deps'])}):")
            for d in result["package_deps"]:
                spec = f" {d['version_spec']}" if d.get("version_spec") else ""
                print(f"  {d['ecosystem']}:{d['name']}{spec}")
    return 0


def cmd_dep_conflicts(args) -> int:
    conn = connect()
    try:
        result = queries.dependency_conflicts(
            conn,
            capability_id_a=args.capability_a,
            capability_id_b=args.capability_b,
        )
    finally:
        conn.close()
    if result is None:
        print("one or both capabilities not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        if not result["conflicts"]:
            print("No version conflicts detected.")
        else:
            print(f"{len(result['conflicts'])} conflict(s):")
            for c in result["conflicts"]:
                print(f"  {c['ecosystem']}:{c['name']}  {c['spec_a']}  vs  {c['spec_b']}")
    return 0


def cmd_verify_status(args) -> int:
    conn = connect()
    try:
        result = queries.verification_status(
            conn,
            capability_id=args.capability_id,
        )
    finally:
        conn.close()
    if result is None:
        print("capability not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Capability: {result['normalized_key']}")
        print(f"Version:    {result.get('display_version', 'n/a')}")
        print(f"Verified:   {result['verified']}")
        if result.get("runs"):
            print(f"\nLatest runs ({len(result['runs'])}):")
            for run in result["runs"]:
                print(f"  [{run['result']}] duration={run.get('duration_ms', 0)}ms "
                      f"hash={run.get('reproducibility_hash', '')}")
                for a in run.get("assertions", []):
                    status = "PASS" if a["passed"] else "FAIL"
                    reason = f" — {a['reason']}" if a.get("reason") else ""
                    print(f"    [{status}] {a['assertion_kind']}{reason}")
        else:
            print("\nNo verification runs yet.")
    return 0


def cmd_experience(args) -> int:
    conn = connect()
    try:
        result = queries.experience_summary(
            conn,
            project_id=args.project_id,
            capability_id=args.capability_id,
        )
    finally:
        conn.close()
    if result is None:
        print("capability or project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Capability:  {result['normalized_key']}")
        print(f"Active uses: {result['active_uses']}")
        print(f"Failures:    {result['total_failures']} "
              f"({result['unresolved_failures']} unresolved)")
        print(f"Score:       {result['experience_score']:.2f}")
        print(f"Recommend:   {result['recommendation']}")
    return 0


def cmd_find_replacement(args) -> int:
    conn = connect()
    try:
        result = queries.find_replacement(
            conn,
            project_id=args.project_id,
            capability_id=args.capability_id,
            failure_kind=args.failure_kind or "other",
            severity=args.severity or "error",
            summary=args.summary or "",
            limit=args.limit,
        )
    finally:
        conn.close()
    if result is None:
        print("capability or project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Failed: {result['normalized_key']}")
        d = result["diagnosis"]
        print(f"Category: {d['category']}  Urgency: {d['urgency']}")
        print(f"Hint: {d['root_cause_hint']}")
        print(f"Recommendation: {result['recommendation']}")
        if result["candidates"]:
            print(f"\nAlternatives ({len(result['candidates'])}):")
            for c in result["candidates"]:
                print(f"  {c['score']:.3f}  {c['normalized_key']}  ({c['reason']})")
        else:
            print("\nNo alternatives found in registry.")
    return 0


def cmd_import_requirements(args) -> int:
    yaml_text = Path(args.file).read_text(encoding="utf-8")
    conn = connect()
    try:
        result = queries.import_requirements(
            conn,
            project_id=args.project_id,
            yaml_text=yaml_text,
        )
    finally:
        conn.close()
    if result is None:
        print("project not found")
        return 1
    if result.get("error"):
        print(f"parse error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Created: {result['created']}  Updated: {result['updated']}  "
              f"Total: {result['total']}")
        for slug in result["requirements"]:
            print(f"  - {slug}")
    return 0


def cmd_requirement_coverage(args) -> int:
    conn = connect()
    try:
        result = queries.requirement_coverage(
            conn,
            project_id=args.project_id,
        )
    finally:
        conn.close()
    if result is None:
        print("project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Requirements: {result['total_requirements']}")
        print(f"Covered:      {result['covered']} ({result['coverage_pct']}%)")
        print(f"Verified:     {result['verified']}")
        print(f"Experience:   {result['with_experience']}")
        if result["requirements"]:
            print()
            for r in result["requirements"]:
                rec = "YES" if r["has_recommendation"] else "no "
                ver = "YES" if r["has_verification"] else "no "
                exp = "YES" if r["has_experience"] else "no "
                verdict = f" [{r['verdict']}]" if r["verdict"] else ""
                print(f"  {r['slug']:30s}  rec={rec}  ver={ver}  exp={exp}{verdict}")
    return 0


def cmd_inspection_history(args) -> int:
    conn = connect()
    try:
        result = queries.inspection_history(
            conn,
            capability_id=args.capability_id,
            extractor_name=args.extractor,
            limit=args.limit,
        )
    finally:
        conn.close()
    if result is None:
        print("capability not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Capability: {result['normalized_key']}")
        if result.get("extractors_covered"):
            print(f"Covered:    {', '.join(result['extractors_covered'])}")
        if result.get("extractors_missing"):
            print(f"Missing:    {', '.join(result['extractors_missing'])}")
        if result["runs"]:
            print(f"\nRuns ({len(result['runs'])}):")
            for r in result["runs"]:
                status = r["status"].upper()
                ev = f" evidence={r['evidence_count']}" if r["evidence_count"] else ""
                err = f" error={r['error_detail']}" if r.get("error_detail") else ""
                print(f"  [{status}] {r['extractor_name']} v{r['extractor_version']}"
                      f" rev={r.get('source_revision', 'n/a')}{ev}{err}")
        else:
            print("\nNo inspection runs yet.")
    return 0


def cmd_authorize_build(args) -> int:
    conn = connect()
    try:
        result = queries.authorize_build(
            conn,
            recommendation_id=args.recommendation_id,
            authorized_by=args.authorized_by,
            reason=args.reason,
        )
    finally:
        conn.close()
    if result is None:
        print("bad recommendation id")
        return 1
    if result.get("error"):
        print(f"refused: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Authorized: {result['build_authorization_id']}")
        print(f"  By: {result['authorized_by']}")
        print(f"  Status: {result['status']}")
    return 0


def cmd_build_gate_check(args) -> int:
    conn = connect()
    try:
        result = queries.build_gate_check(
            conn,
            project_requirement_id=args.requirement_id,
        )
    finally:
        conn.close()
    if result is None:
        print("bad requirement id")
        return 1
    if args.json:
        _print_json(result)
    else:
        status = "ALLOWED" if result["allowed"] else "DENIED"
        print(f"[{status}] {result['reason']}")
    return 0


def cmd_build_coverage(args) -> int:
    conn = connect()
    try:
        result = queries.build_coverage(
            conn,
            project_id=args.project_id,
        )
    finally:
        conn.close()
    if result is None:
        print("project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Requirements: {result['total_requirements']}")
        print(f"BUILD verdicts: {result['build_verdicts']}")
        print(f"Authorized:    {result['authorized']} ({result['authorization_pct']}%)")
        if result["requirements"]:
            print()
            for r in result["requirements"]:
                verdict = r.get("verdict") or "none"
                auth = "AUTH" if r["has_authorization"] else "    "
                print(f"  {r['slug']:30s}  verdict={verdict:10s}  {auth}")
    return 0


def cmd_create_adapter(args) -> int:
    io_transform = None
    if args.io_transform:
        io_transform = json.loads(args.io_transform)
    conn = connect()
    try:
        result = queries.create_adapter_spec(
            conn,
            source_id=args.source_id,
            target_id=args.target_id,
            adapter_hint=args.adapter_hint or "",
            io_transform=io_transform,
        )
    finally:
        conn.close()
    if result is None:
        print("capability not found or pair already exists")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Adapter: {result['bridge_kind']}")
        print(f"  {result['source_runtime']} -> {result['target_runtime']}")
        print(f"  Status: {result['status']}")
        print(f"  {result['description']}")
        if args.show_code:
            print(f"\n--- Skeleton Code ---\n{result['skeleton_code']}")
            print(f"\n--- Test Code ---\n{result['test_code']}")
    return 0


def cmd_list_adapters(args) -> int:
    conn = connect()
    try:
        rows = queries.list_adapter_specs(
            conn,
            capability_id=args.capability_id,
            status=args.status,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
    else:
        _print_row_table(rows, ["source_key", "target_key", "bridge_kind",
                                "status", "source_runtime", "target_runtime"])
    return 0


def cmd_ingest_pdr(args) -> int:
    pdr_text = Path(args.file).read_text(encoding="utf-8")
    conn = connect()
    try:
        result = pdr_queries.ingest_pdr(
            conn, project_name=args.project_name,
            pdr_title=args.title or Path(args.file).stem,
            pdr_text=pdr_text,
            project_id=getattr(args, "project_id", None),
        )
    finally:
        conn.close()
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        dup = " (duplicate)" if result.get("was_duplicate") else ""
        print(f"Project:  {result['project_id']}")
        print(f"Source:   {result['requirement_source_id']}{dup}")
        print(f"Hash:     {result['content_hash'][:16]}...")
    return 0


def cmd_record_requirements(args) -> int:
    reqs_text = Path(args.file).read_text(encoding="utf-8")
    reqs = json.loads(reqs_text)
    if isinstance(reqs, dict):
        reqs = reqs.get("requirements", [reqs])
    conn = connect()
    try:
        result = pdr_queries.record_requirements(
            conn, project_id=args.project_id,
            requirement_source_id=args.source_id,
            requirements=reqs,
        )
    finally:
        conn.close()
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        for r in result["requirements"]:
            status = "OK" if r["valid"] else "INVALID"
            slug = r.get("slug", "?")
            errs = "; ".join(r.get("errors", []))
            extra = f" — {errs}" if errs else ""
            print(f"  [{status}] {slug}{extra}")
    return 0


def cmd_pdr_lock(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.lock_architecture(
            conn, project_id=args.project_id,
            approved_by=args.by, approval_note=args.note,
        )
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if result.get("refused"):
        print(f"Refused: {result['reason']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Lock v{result['version']}: {result['architecture_lock_id']}")
        print(f"  Decisions: {result['decision_count']}")
        print(f"  Hash: {result['source_lock_hash'][:16]}...")
    return 0


def cmd_pdr_coverage(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.coverage(conn, project_id=args.project_id)
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Project: {result['project_name']}")
        print(f"  Requirements: {result['total_requirements']}")
        print(f"  Decided:      {result['decided']}")
        print(f"  Tasked:       {result['tasked']}")
        print(f"  Implemented:  {result['implemented']}")
        print(f"  Verified:     {result['verified']}")
    return 0


def cmd_search_for_requirement(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.search_for_requirement(
            conn, req_id=args.req_id,
            search_query=args.query,
            ecosystem=args.ecosystem,
            capability_kind=args.capability_kind,
            component_kind=args.kind,
            runtime=args.runtime,
            cost_tier=args.cost_tier,
            limit=args.limit,
        )
    finally:
        conn.close()
    if result is None:
        print("Requirement not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Requirement: {result['slug']} — {result['description']}")
        if result["constraints"]:
            print(f"Constraints: {len(result['constraints'])}")
        print(f"Search query: {result.get('search_query', '(none)')}")
        print(f"Candidates found: {result['candidate_count']}")
        for c in result["candidates"]:
            score = f"fit={c['fit_score']:.3f}"
            gaps = f"gaps={c['blocking_gap_count']}"
            intrinsic = f"score={c['intrinsic_score']:.3f}" if c['intrinsic_score'] else "score=n/a"
            print(f"  {c['display_name']} ({c['ecosystem']}) — {score}, {gaps}, {intrinsic}")
    return 0


def cmd_record_decision(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.record_decision(
            conn, req_id=args.req_id,
            verdict=args.verdict.upper(),
            chosen_capability_version_id=args.capability_version_id,
            rationale=args.rationale or "",
        )
    finally:
        conn.close()
    if result is None:
        print("Requirement not found")
        return 1
    if result.get("refused"):
        print(f"Refused: {result['reason']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Recorded: {result['verdict']} → recommendation {result['recommendation_id']}")
    return 0


def cmd_check_against_lock(args) -> int:
    proposed = {"description": args.description or ""}
    if args.capability_version_id:
        proposed["capability_version_id"] = args.capability_version_id
    if args.provider:
        proposed["provider"] = args.provider

    conn = connect()
    try:
        result = pdr_queries.check_against_lock(
            conn, project_id=args.project_id,
            proposed_change=proposed,
        )
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Verdict: {result['verdict']}")
        print(f"Reason: {result['reason']}")
    return 0


def cmd_record_build_progress(args) -> int:
    conn = connect()
    try:
        if args.build_task_id:
            verification = None
            if args.verify_req_id and args.verify_outcome:
                verification = {
                    "req_id": args.verify_req_id,
                    "outcome": args.verify_outcome,
                    "evidence_ref": args.verify_evidence,
                }
            result = pdr_queries.record_build_progress(
                conn,
                build_task_id=args.build_task_id,
                status=args.status,
                target_commit=args.target_commit,
                verification=verification,
            )
        else:
            req_ids = [r.strip() for r in args.req_ids.split(",")]
            result = pdr_queries.record_build_progress(
                conn,
                project_id=args.project_id,
                architecture_lock_id=args.lock_id,
                title=args.title,
                objective=args.objective,
                req_ids=req_ids,
            )
    finally:
        conn.close()
    if result is None:
        print("Not found")
        return 1
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Task {result['build_task_id']} — status: {result['status']}")
    return 0


def cmd_pdr_brief(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.build_approval_brief(conn, project_id=args.project_id)
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(result["brief_markdown"])
    return 0


def cmd_fair_extract(args) -> int:
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import extract_requirements
    pdr_text = Path(args.file).read_text(encoding="utf-8")
    client = FairClient()
    result = extract_requirements(client, pdr_text)
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        reqs = result.get("requirements", [])
        print(f"Extracted {len(reqs)} requirements "
              f"(via {result.get('provider_id', '?')}/{result.get('model_id', '?')})")
        for i, r in enumerate(reqs, 1):
            prio = r.get("priority", "?")
            print(f"  {i}. [{prio}] {r.get('text', '(no text)')}")
    return 0


def cmd_fair_suggest_verdict(args) -> int:
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import suggest_verdict
    candidates_data = json.loads(Path(args.candidates_file).read_text(encoding="utf-8"))
    if isinstance(candidates_data, dict):
        candidates_data = candidates_data.get("candidates", [candidates_data])
    client = FairClient()
    result = suggest_verdict(
        client,
        req_id=args.req_id,
        description=args.description,
        constraints=json.loads(args.constraints) if args.constraints else [],
        candidates=candidates_data,
    )
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        print(f"Verdict: {result.get('verdict', '?')}")
        print(f"Rationale: {result.get('rationale', '')}")
        if result.get("chosen_capability_version_id"):
            print(f"Chosen: {result['chosen_capability_version_id']}")
    return 0


def cmd_fair_analyze(args) -> int:
    from integrations.fair_client import FairClient
    from integrations.fair_pdr import analyze_source
    source_code = Path(args.file).read_text(encoding="utf-8")
    client = FairClient()
    result = analyze_source(client, source_code, file_path=args.file)
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        caps = result.get("capabilities", [])
        print(f"Found {len(caps)} capabilities "
              f"(via {result.get('provider_id', '?')}/{result.get('model_id', '?')})")
        for c in caps:
            ifaces = len(c.get("interfaces", []))
            deps = len(c.get("dependencies", []))
            print(f"  {c.get('name', '?')} ({c.get('kind', '?')}) "
                  f"— {ifaces} interfaces, {deps} deps")
    return 0


def cmd_fair_status(args) -> int:
    from integrations.fair_client import FairClient
    client = FairClient()
    available = client.is_available()
    if args.json:
        _print_json({"available": available, "api_url": client.api_url,
                      "client_id": client.client_id})
    else:
        status = "ONLINE" if available else "OFFLINE"
        print(f"FAIR: {status}")
        print(f"  URL: {client.api_url}")
        print(f"  Client: {client.client_id}")
    return 0


def cmd_eval_record(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    conn = connect()
    try:
        result = eval_queries.record_evaluation(
            conn,
            asset_slug=payload["asset_slug"],
            verdict=payload["verdict"],
            asset_type=payload.get("asset_type", "repo"),
            url=payload.get("url"),
            code_quality=payload.get("code_quality"),
            banked=payload.get("banked", False),
            summary=payload.get("summary", ""),
            code_quality_notes=payload.get("code_quality_notes", ""),
            useful_parts=payload.get("useful_parts", ""),
            skip_notes=payload.get("skip_notes", ""),
            banked_references=payload.get("banked_references"),
            projects=payload.get("projects"),
            problem_types=payload.get("problem_types"),
            license_notes=payload.get("license_notes", ""),
            cip_status=payload.get("cip_status", "not-ingested"),
            evaluated_at=payload.get("evaluated_at"),
        )
    finally:
        conn.close()
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        b = " [banked]" if result["banked"] else ""
        print(f"Recorded: {result['asset_slug']} — {result['verdict']}{b}")
    return 0


def cmd_eval_search(args) -> int:
    problem_types = [t.strip() for t in args.problem_types.split(",")
                     if t.strip()] if args.problem_types else None
    conn = connect()
    try:
        rows = eval_queries.search_evaluations(
            conn,
            query=args.query,
            problem_types=problem_types,
            verdict=args.verdict,
            asset_type=args.asset_type,
            banked=(True if args.banked else None),
            project=args.project,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
    else:
        if not rows:
            print("(no evaluations)")
        for r in rows:
            b = " [banked]" if r["banked"] else ""
            tags = ", ".join(r.get("problem_types") or [])
            print(f"  {r['asset_slug']:35s}  {r['verdict']:10s}{b}  {tags}")
    return 0


def cmd_eval_get(args) -> int:
    conn = connect()
    try:
        result = eval_queries.get_evaluation(conn, asset_slug=args.asset_slug)
    finally:
        conn.close()
    if result is None:
        print("Evaluation not found")
        return 1
    _print_json(result)
    return 0


def cmd_info(args) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT component_kind, COUNT(*) FROM capability "
                "GROUP BY component_kind ORDER BY 2 DESC"
            )
            by_kind = cur.fetchall()
            cur.execute(
                "SELECT cost_tier, COUNT(*) FROM capability "
                "GROUP BY cost_tier ORDER BY 2 DESC"
            )
            by_cost = cur.fetchall()
            cur.execute(
                "SELECT normalized_key, total_score FROM component_score "
                "JOIN capability ON capability.id = component_score.capability_id "
                "ORDER BY total_score DESC LIMIT 10"
            )
            top10 = cur.fetchall()
    finally:
        conn.close()
    print("Components by kind:")
    for k, n in by_kind:
        print(f"  {k:20s}  {n}")
    print("\nComponents by cost tier:")
    for c, n in by_cost:
        print(f"  {str(c):20s}  {n}")
    print("\nTop 10 by health score:")
    for key, s in top10:
        print(f"  {float(s):.3f}  {key}")
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def _add_filters(sub, include_kind: bool = True) -> None:
    if include_kind:
        sub.add_argument("--kind", default=None,
                          help="Filter by component_kind (library, repo, agent, skill, mcp_tool, workflow_template).")
    sub.add_argument("--ecosystem", default=None,
                      help="Filter by ecosystem (pypi, npm, source).")
    sub.add_argument("--runtime", default=None,
                      help="Filter by runtime (python_import, mcp_stdio, ...).")
    sub.add_argument("--cost-tier", dest="cost_tier", default=None,
                      help="Filter by cost_tier (free, free_tier, cheap_paid, paid).")
    sub.add_argument("--license-status", dest="license_status", default=None,
                      help="Filter by license_status (verified_open_source, needs_review, blocked, unknown).")
    sub.add_argument("--project-id", dest="project_id", default=None,
                      help="UUID of a project — applies its project_constraint set.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cip-composer", description=__doc__)
    subs = p.add_subparsers(dest="cmd", required=True)

    f = subs.add_parser("find", help="Keyword search the registry.")
    f.add_argument("query")
    _add_filters(f)
    f.add_argument("--tags", default=None,
                    help="Comma-separated topic tags to filter by (e.g. 'video,ai').")
    f.add_argument("--limit", type=int, default=20)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

    b = subs.add_parser("browse", help="Browse without a keyword.")
    _add_filters(b)
    b.add_argument("--limit", type=int, default=50)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_browse)

    s = subs.add_parser("suggest", help="Decompose an intent into a pipeline.")
    s.add_argument("intent")
    s.add_argument("--project-id", dest="project_id", default=None)
    s.add_argument("--max-stages", dest="max_stages", type=int, default=5)
    s.add_argument("--name", default=None)
    s.add_argument("--out", default=None,
                    help="Write proposal JSON to this path instead of stdout.")
    s.set_defaults(func=cmd_suggest)

    sc = subs.add_parser("scaffold", help="Turn a proposal into a real repo skeleton.")
    sc.add_argument("--proposal", required=True)
    sc.add_argument("--out-dir", dest="out_dir", required=True)
    sc.set_defaults(func=cmd_scaffold)

    fl = subs.add_parser("flow", help="Suggest -> save -> scaffold in one command.")
    fl.add_argument("intent")
    fl.add_argument("--out-dir", dest="out_dir", required=True)
    fl.add_argument("--project-id", dest="project_id", default=None)
    fl.add_argument("--max-stages", dest="max_stages", type=int, default=5)
    fl.add_argument("--name", default=None)
    fl.set_defaults(func=cmd_flow)

    ss = subs.add_parser("search-symbols", help="Search for symbols across the registry.")
    ss.add_argument("query")
    ss.add_argument("--symbol-kind", dest="symbol_kind", default=None,
                     help="Filter by symbol_kind (function, class, method, ...).")
    ss.add_argument("--role", default=None,
                     help="Filter by role (utility, data_model, entry_point, ...).")
    ss.add_argument("--language", default=None, help="Filter by language (python).")
    ss.add_argument("--capability-id", dest="capability_id", default=None,
                     help="Restrict to symbols from one capability UUID.")
    ss.add_argument("--limit", type=int, default=30)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_search_symbols)

    ig = subs.add_parser("ingest-symbols",
                          help="Materialize symbol evidence into capability_symbol rows.")
    ig.add_argument("capability_version_id",
                     help="UUID of the capability version to populate.")
    ig.add_argument("--source-revision-id", dest="source_revision_id", default=None,
                     help="Optional: scope to evidence from one source revision.")
    ig.add_argument("--json", action="store_true")
    ig.set_defaults(func=cmd_ingest_symbols)

    al = subs.add_parser("analyze-local",
                          help="Run all extractors against local files.")
    al.add_argument("root", help="Directory path to scan.")
    al.add_argument("--max-files", dest="max_files", type=int, default=5000,
                     help="Max files to collect (default 5000).")
    al.add_argument("--include-items", dest="include_items", action="store_true",
                     help="Include individual evidence items in output.")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=cmd_analyze_local)

    le = subs.add_parser("list-extractors",
                          help="List all available evidence extractors.")
    le.add_argument("--json", action="store_true")
    le.set_defaults(func=cmd_list_extractors)

    df = subs.add_parser("dep-fit", help="Check dependency fit against an environment.")
    df.add_argument("capability_id", help="UUID of the capability to check.")
    df.add_argument("--project-id", dest="project_id", default=None,
                     help="UUID of a project whose environment_profile to check against.")
    df.add_argument("--profile-name", dest="profile_name", default="default",
                     help="Which environment profile to use (default: 'default').")
    df.add_argument("--json", action="store_true")
    df.set_defaults(func=cmd_dep_fit)

    dc = subs.add_parser("dep-conflicts", help="Check version conflicts between two capabilities.")
    dc.add_argument("capability_a", help="UUID of first capability.")
    dc.add_argument("capability_b", help="UUID of second capability.")
    dc.add_argument("--json", action="store_true")
    dc.set_defaults(func=cmd_dep_conflicts)

    fr = subs.add_parser("find-replacement", help="Diagnose failure and find replacement candidates.")
    fr.add_argument("project_id", help="UUID of the project.")
    fr.add_argument("capability_id", help="UUID of the failed capability.")
    fr.add_argument("--failure-kind", dest="failure_kind", default="other",
                     help="Type of failure (install_failure, runtime_error, etc.).")
    fr.add_argument("--severity", default="error",
                     help="Severity: warning, error, critical.")
    fr.add_argument("--summary", default="",
                     help="Free-text failure description.")
    fr.add_argument("--limit", type=int, default=10)
    fr.add_argument("--json", action="store_true")
    fr.set_defaults(func=cmd_find_replacement)

    ex = subs.add_parser("experience", help="Experience summary for a capability in a project.")
    ex.add_argument("project_id", help="UUID of the project.")
    ex.add_argument("capability_id", help="UUID of the capability.")
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_experience)

    vs = subs.add_parser("verify-status", help="Check verification status of a capability.")
    vs.add_argument("capability_id", help="UUID of the capability to check.")
    vs.add_argument("--json", action="store_true")
    vs.set_defaults(func=cmd_verify_status)

    ir = subs.add_parser("import-requirements",
                          help="Import requirements from a YAML file into a project.")
    ir.add_argument("project_id", help="UUID of the project.")
    ir.add_argument("file", help="Path to the YAML requirements file.")
    ir.add_argument("--json", action="store_true")
    ir.set_defaults(func=cmd_import_requirements)

    rc = subs.add_parser("requirement-coverage",
                          help="Show requirement coverage report for a project.")
    rc.add_argument("project_id", help="UUID of the project.")
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=cmd_requirement_coverage)

    ih = subs.add_parser("inspection-history",
                          help="Show inspection run history for a capability.")
    ih.add_argument("capability_id", help="UUID of the capability.")
    ih.add_argument("--extractor", default=None,
                     help="Filter by extractor (manifests, licenses, interfaces, symbols, secrets, tests).")
    ih.add_argument("--limit", type=int, default=20)
    ih.add_argument("--json", action="store_true")
    ih.set_defaults(func=cmd_inspection_history)

    ab = subs.add_parser("authorize-build",
                          help="Authorize code generation for a BUILD recommendation.")
    ab.add_argument("recommendation_id", help="UUID of the BUILD recommendation.")
    ab.add_argument("--by", dest="authorized_by", required=True,
                     help="Who is authorizing (actor name).")
    ab.add_argument("--reason", required=True,
                     help="Why build is authorized (audit trail).")
    ab.add_argument("--json", action="store_true")
    ab.set_defaults(func=cmd_authorize_build)

    bg = subs.add_parser("build-gate-check",
                          help="Check if code generation is allowed for a requirement.")
    bg.add_argument("requirement_id", help="UUID of the project_requirement.")
    bg.add_argument("--json", action="store_true")
    bg.set_defaults(func=cmd_build_gate_check)

    bc = subs.add_parser("build-coverage",
                          help="Build authorization coverage report for a project.")
    bc.add_argument("project_id", help="UUID of the project.")
    bc.add_argument("--json", action="store_true")
    bc.set_defaults(func=cmd_build_coverage)

    ca = subs.add_parser("create-adapter",
                          help="Create an adapter spec between two capabilities.")
    ca.add_argument("source_id", help="UUID of the upstream capability.")
    ca.add_argument("target_id", help="UUID of the downstream capability.")
    ca.add_argument("--adapter-hint", dest="adapter_hint", default="",
                     help="Optional bridge hint from compatibility check.")
    ca.add_argument("--io-transform", dest="io_transform", default=None,
                     help="JSON string describing I/O type mapping.")
    ca.add_argument("--show-code", dest="show_code", action="store_true",
                     help="Print the generated skeleton code.")
    ca.add_argument("--json", action="store_true")
    ca.set_defaults(func=cmd_create_adapter)

    la = subs.add_parser("list-adapters",
                          help="List adapter specifications.")
    la.add_argument("--capability-id", dest="capability_id", default=None,
                     help="Filter by capability UUID (as source or target).")
    la.add_argument("--status", default=None,
                     help="Filter by status (draft, generated, reviewed, tested).")
    la.add_argument("--limit", type=int, default=50)
    la.add_argument("--json", action="store_true")
    la.set_defaults(func=cmd_list_adapters)

    ip = subs.add_parser("ingest-pdr", help="Ingest a PDR document into a project.")
    ip.add_argument("file", help="Path to the PDR text file.")
    ip.add_argument("--project-name", dest="project_name", required=True,
                     help="Project name (created if new).")
    ip.add_argument("--title", default=None, help="PDR title (defaults to filename).")
    ip.add_argument("--project-id", dest="project_id", default=None,
                     help="Existing project UUID.")
    ip.add_argument("--json", action="store_true")
    ip.set_defaults(func=cmd_ingest_pdr)

    rr = subs.add_parser("record-requirements",
                          help="Record extracted requirements from a PDR.")
    rr.add_argument("project_id", help="UUID of the project.")
    rr.add_argument("source_id", help="UUID of the requirement_source.")
    rr.add_argument("file", help="JSON file with requirements array.")
    rr.add_argument("--json", action="store_true")
    rr.set_defaults(func=cmd_record_requirements)

    pl = subs.add_parser("pdr-lock", help="Lock the architecture for a project.")
    pl.add_argument("project_id", help="UUID of the project.")
    pl.add_argument("--by", dest="by", required=True, help="Who is approving.")
    pl.add_argument("--note", default=None, help="Approval note.")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_pdr_lock)

    pc = subs.add_parser("pdr-coverage", help="PDR implementation coverage report.")
    pc.add_argument("project_id", help="UUID of the project.")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_pdr_coverage)

    pb = subs.add_parser("pdr-brief", help="Render the approval brief for a project.")
    pb.add_argument("project_id", help="UUID of the project.")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=cmd_pdr_brief)

    sr = subs.add_parser("search-for-requirement",
                          help="Search registry and compute fit for a requirement.")
    sr.add_argument("req_id", help="UUID of the project_requirement.")
    sr.add_argument("--query", default=None,
                     help="Search query (defaults to requirement description).")
    sr.add_argument("--ecosystem", default=None)
    sr.add_argument("--capability-kind", dest="capability_kind", default=None)
    sr.add_argument("--kind", default=None, help="Filter by component_kind.")
    sr.add_argument("--runtime", default=None)
    sr.add_argument("--cost-tier", dest="cost_tier", default=None)
    sr.add_argument("--limit", type=int, default=20)
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=cmd_search_for_requirement)

    rd = subs.add_parser("record-decision",
                          help="Record a reuse/build verdict for a requirement.")
    rd.add_argument("req_id", help="UUID of the project_requirement.")
    rd.add_argument("verdict", help="ADOPT, ADAPT, WRAP, REFERENCE, REJECT, or BUILD.")
    rd.add_argument("--capability-version-id", dest="capability_version_id", default=None,
                     help="Required for ADOPT/ADAPT/WRAP/REFERENCE.")
    rd.add_argument("--rationale", default=None, help="Free-text reason.")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(func=cmd_record_decision)

    ca_lock = subs.add_parser("check-against-lock",
                               help="Check a proposed change against the architecture lock.")
    ca_lock.add_argument("project_id", help="UUID of the project.")
    ca_lock.add_argument("--description", default=None, help="Description of the change.")
    ca_lock.add_argument("--capability-version-id", dest="capability_version_id", default=None)
    ca_lock.add_argument("--provider", default=None, help="Provider name to check.")
    ca_lock.add_argument("--json", action="store_true")
    ca_lock.set_defaults(func=cmd_check_against_lock)

    bp = subs.add_parser("record-build-progress",
                          help="Create or update a build task.")
    bp.add_argument("--build-task-id", dest="build_task_id", default=None,
                     help="UUID of existing task (update mode).")
    bp.add_argument("--project-id", dest="project_id", default=None,
                     help="UUID of project (create mode).")
    bp.add_argument("--lock-id", dest="lock_id", default=None,
                     help="UUID of architecture_lock (create mode).")
    bp.add_argument("--title", default=None, help="Task title (create mode).")
    bp.add_argument("--objective", default=None, help="Task objective (create mode).")
    bp.add_argument("--req-ids", dest="req_ids", default=None,
                     help="Comma-separated requirement UUIDs (create mode).")
    bp.add_argument("--status", default=None,
                     help="New status (update mode): planned/in_progress/implemented/verified/abandoned.")
    bp.add_argument("--target-commit", dest="target_commit", default=None)
    bp.add_argument("--verify-req-id", dest="verify_req_id", default=None,
                     help="Requirement UUID to verify (update mode).")
    bp.add_argument("--verify-outcome", dest="verify_outcome", default=None,
                     help="Verification outcome: pass/fail/not_run.")
    bp.add_argument("--verify-evidence", dest="verify_evidence", default=None,
                     help="Evidence reference for verification.")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_record_build_progress)

    fe = subs.add_parser("fair-extract",
                          help="Use FAIR to extract requirements from a PDR.")
    fe.add_argument("file", help="Path to the PDR text file.")
    fe.add_argument("--json", action="store_true")
    fe.set_defaults(func=cmd_fair_extract)

    fsv = subs.add_parser("fair-suggest-verdict",
                           help="Use FAIR to suggest a verdict for a requirement.")
    fsv.add_argument("req_id", help="UUID of the project_requirement.")
    fsv.add_argument("description", help="Requirement description text.")
    fsv.add_argument("candidates_file", help="JSON file with candidates array.")
    fsv.add_argument("--constraints", default=None,
                      help="JSON string of constraint dicts.")
    fsv.add_argument("--json", action="store_true")
    fsv.set_defaults(func=cmd_fair_suggest_verdict)

    fa = subs.add_parser("fair-analyze",
                          help="Use FAIR to analyze source code for capabilities.")
    fa.add_argument("file", help="Path to the source file.")
    fa.add_argument("--json", action="store_true")
    fa.set_defaults(func=cmd_fair_analyze)

    fst = subs.add_parser("fair-status", help="Check FAIR availability.")
    fst.add_argument("--json", action="store_true")
    fst.set_defaults(func=cmd_fair_status)

    er = subs.add_parser("eval-record",
                          help="Record an asset evaluation into the store (from a JSON file).")
    er.add_argument("file", help="JSON file with the evaluation fields.")
    er.add_argument("--json", action="store_true")
    er.set_defaults(func=cmd_eval_record)

    es = subs.add_parser("eval-search",
                          help="Search the evaluation store (prior verdicts + banked references).")
    es.add_argument("--query", default=None, help="Substring across slug/summary/useful_parts.")
    es.add_argument("--problem-types", dest="problem_types", default=None,
                     help="Comma-separated problem-type tags (overlap match).")
    es.add_argument("--verdict", default=None)
    es.add_argument("--asset-type", dest="asset_type", default=None)
    es.add_argument("--project", default=None)
    es.add_argument("--banked", action="store_true", help="Only banked-for-future entries.")
    es.add_argument("--limit", type=int, default=50)
    es.add_argument("--json", action="store_true")
    es.set_defaults(func=cmd_eval_search)

    eg = subs.add_parser("eval-get", help="Fetch one evaluation by slug.")
    eg.add_argument("asset_slug", help="The unique slug.")
    eg.set_defaults(func=cmd_eval_get)

    i = subs.add_parser("info", help="Registry stats.")
    i.set_defaults(func=cmd_info)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
