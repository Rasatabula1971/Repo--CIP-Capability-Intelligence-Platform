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

from db.connection import connect
from mcp_server import queries
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

    vs = subs.add_parser("verify-status", help="Check verification status of a capability.")
    vs.add_argument("capability_id", help="UUID of the capability to check.")
    vs.add_argument("--json", action="store_true")
    vs.set_defaults(func=cmd_verify_status)

    i = subs.add_parser("info", help="Registry stats.")
    i.set_defaults(func=cmd_info)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
