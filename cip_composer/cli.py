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
import os
import sys
from pathlib import Path
from typing import Any

from analysis.extractors import extractor_names as _extractor_names
from core.pdr.text import WRAP_PREFIX, one_line, printable, wrap_block
from db.connection import connect
from mcp_server import queries
from mcp_server import pdr_approvals
from mcp_server import pdr_queries
from mcp_server import pdr_store
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
    """
    JSON is for a program to read, so it is not wrapped: a wrapped JSON
    document does not parse. ensure_ascii (the default) escapes every
    control character, so it is safe to print as it is.
    """
    text = json.dumps(obj, indent=2, default=str)
    out = sys.stdout
    if isinstance(out, _SafeStream):
        out.write_unwrapped(text + "\n")
    else:
        print(text)


class _SafeStream:
    """
    Standard output with everything that could mislead the person reading
    it taken out. Much of what this CLI prints was written by someone else
    — an agent's requirement text, a project name, a repository's display
    name scraped from the internet — and a person may be about to act on
    what they see. In one place, so no command has to remember to:

      - escape sequences and other control characters are removed;
      - with `wrap`, every line is wrapped here to the terminal's width,
        and each continuation row starts with WRAP_PREFIX. Otherwise a
        long value pushes whatever follows it onto a row of its own,
        where it reads as a line CIP printed. A summary was wrapped; the
        "Project: <name>" line under it was not, and a padded name put a
        forged "PDR file: ... matches" row on the approval screen.
    """

    def __init__(self, stream, wrap: bool = False):
        self._stream = stream
        self._wrap = wrap
        self._pending = ""

    def _emit(self, line: str) -> None:
        self._stream.write(wrap_block(line, _terminal_width()) if self._wrap else line)

    def write(self, text):
        if not isinstance(text, str):
            return self._stream.write(text)
        self._pending += printable(text)
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._emit(line)
            self._stream.write("\n")
        return len(text)

    def write_unwrapped(self, text: str) -> None:
        self.flush()
        self._stream.write(printable(text))

    def flush(self):
        # A prompt has no newline; it must still appear before the read.
        if self._pending:
            self._emit(self._pending)
            self._pending = ""
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


# Commands a person reads to decide something, or that print text an agent
# wrote. Their output is always wrapped, also when piped into a pager. The
# rest are wrapped only when writing to a terminal, so `cip-composer find
# ... | grep` still gets one row per line.
_ALWAYS_WRAPPED = frozenset({
    "cmd_pdr_status", "cmd_ingest_pdr", "cmd_record_requirements", "cmd_record_needs",
    "cmd_confirm_requirements", "cmd_pdr_lock", "cmd_pdr_coverage",
    "cmd_search_for_requirement", "cmd_record_decision", "cmd_check_against_lock",
    "cmd_record_build_progress", "cmd_pdr_brief", "cmd_approvals_list",
    "cmd_approvals_show", "cmd_approvals_approve", "cmd_approvals_reject", "cmd_verify",
})


def _terminal_width() -> int:
    """
    Columns to wrap to: the narrowest terminal attached to this process,
    less one.

    The terminal itself is asked. COLUMNS in the environment can be stale
    or simply wrong, and a figure larger than the real width is the unsafe
    direction: CIP would leave a line long and the terminal would wrap it.
    All three standard streams are tried, so output piped into a pager is
    still wrapped for the terminal the pager draws on. With no terminal
    at all, 80: narrower than the real thing is harmless.
    """
    widths = []
    for stream in (sys.__stdout__, sys.__stderr__, sys.__stdin__):
        try:
            widths.append(os.get_terminal_size(stream.fileno()).columns)
        except (OSError, ValueError, AttributeError):
            continue
    columns = min(widths) if widths else 80
    return max(len(WRAP_PREFIX) + 20, columns - 1)


def _print_block(text: str) -> None:
    """
    Print a summary a person will decide on. The output stream wraps every
    line it is given (see _SafeStream); this exists so the places that
    print a summary say so.
    """
    print(text)


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


class _InputError(Exception):
    """A problem with a file the user pointed the CLI at. Printed, not raised."""


def _load_json_arg(path: str | None) -> Any:
    """
    Read a JSON file. Accepts UTF-8 with or without a byte-order mark and
    UTF-16 with one (what PowerShell 5.1 writes with `>`).
    """
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise _InputError(f"could not read {path}: {e}") from None
    encoding = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        return json.loads(data.decode(encoding))
    except UnicodeDecodeError:
        raise _InputError(
            f"{path} is not UTF-8 (or UTF-16 with a byte-order mark) text"
        ) from None
    except json.JSONDecodeError as e:
        raise _InputError(f"{path} is not valid JSON: {e}") from None


def _abs_path(path: str | None) -> str | None:
    """
    Absolute form of a document path given on the command line. The CLI
    runs where the person is standing, so relative paths resolve from
    there; the tool layer itself only accepts absolute paths.
    """
    return str(Path(path).expanduser().resolve()) if path else None


def _gate_result(result: dict | None, missing: str) -> int | None:
    """Shared handling for the three non-success shapes. None = carry on."""
    if result is None:
        print(missing)
        return 1
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if result.get("refused"):
        print(f"Refused: {result['reason']}")
        return 1
    return None


def cmd_pdr_status(args) -> int:
    conn = connect()
    try:
        is_id = pdr_store.is_uuid(args.project)
        result = pdr_queries.pdr_status(
            conn,
            project_id=args.project if is_id else None,
            project_name=None if is_id else args.project,
        )
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if args.json:
        _print_json(result)
        return 0
    print(f"Project: {one_line(result['project_name'], 120)}  ({result['project_id']})")
    print(f"Phase:   {result['phase']}")
    req = result["requirements"]
    print(f"Requirements: {len(req['items'])} recorded, confirmation: {req['confirmation']}")
    for b in req["blockers"]:
        print(f"  blocker: {one_line(b, 400)}")
    for n in result["needs"]:
        state = n["verdict"] or ("searched" if n["swept"] else "not searched")
        print(f"  need {one_line(n['slug'], 100)}: {state}")
    lock = result["lock"]
    if lock:
        usable = "usable" if lock["usable"] else "STALE"
        print(f"Lock: v{lock['version']} approved by {one_line(lock['approved_by'], 120)} "
              f"({usable})")
        for r in lock["stale_reasons"]:
            print(f"  stale: {one_line(r, 400)}")
    else:
        print("Lock: none")
    for p in result["pending_approvals"]:
        print(f"Awaiting your approval: {p['kind']}  {p['approval_request_id'][:8]}")
    if result.get("obsolete_requests"):
        print(f"Obsolete approval requests (overtaken by later edits): "
              f"{result['obsolete_requests']}")
    approved = result.get("approved_lock_request")
    if approved:
        print(f"Lock request {approved['approval_request_id'][:8]} is approved "
              f"and not yet written.")
    p = result["progress"]
    print(f"Progress: {p['verified']}/{p['in_scope']} in-scope requirements verified "
          f"({p['verified_cip_executed']} by CIP-executed tests)")
    print("Next:")
    for a in result["next_actions"]:
        print(f"  - {one_line(a, 600)}")
    return 0


def cmd_ingest_pdr(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.ingest_pdr(
            conn, project_name=args.project_name,
            pdr_title=args.title or Path(args.file).stem,
            pdr_path=_abs_path(args.file),
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
        if result.get("orphaned"):
            print("Orphaned requirements (quote not in the new text): "
                  + ", ".join(result["orphaned"]))
    return 0


def cmd_record_requirements(args) -> int:
    reqs = _load_json_arg(args.file)
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
        cov = result.get("span_coverage")
        if cov and cov["unmapped_sections"]:
            print("PDR sections with no requirement mapped: "
                  + ", ".join(one_line(s["heading"], 80) for s in cov["unmapped_sections"]))
    # Exit status says whether everything was recorded, so a script (or an
    # agent reading only the status) cannot take a refusal for success.
    return 0 if all(r["valid"] for r in result["requirements"]) else 2


def cmd_record_needs(args) -> int:
    needs = _load_json_arg(args.file)
    if isinstance(needs, dict):
        needs = needs.get("needs", [needs])
    conn = connect()
    try:
        result = pdr_queries.record_capability_needs(
            conn, project_id=args.project_id, needs=needs,
        )
    finally:
        conn.close()
    rc = _gate_result(result, "Project not found")
    if rc is not None:
        return rc
    if args.json:
        _print_json(result)
    else:
        for n in result["needs"]:
            status = "OK" if n["valid"] else "INVALID"
            notes = "; ".join(n.get("errors", []) + n.get("warnings", []))
            print(f"  [{status}] {n.get('slug', '?')}" + (f" — {notes}" if notes else ""))
        for b in result["blockers"]:
            print(f"  blocker: {one_line(b, 400)}")
    return 0 if all(n["valid"] for n in result["needs"]) else 2


def cmd_confirm_requirements(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.request_requirements_confirmation(
            conn, project_id=args.project_id,
        )
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    done = 0 if result["status"] in ("awaiting_approval", "approved") else 2
    if args.json:
        _print_json(result)
        return done
    print(f"Status: {result['status']}")
    for b in result.get("blockers", []):
        print(f"  blocker: {one_line(b, 400)}")
    if result.get("approval_request_id"):
        print(f"Approval request: {result['approval_request_id']}")
    if result.get("how_to_approve"):
        print(result["how_to_approve"])
    return done


_LOCK_INPUT_KEYS = ("approved_providers", "cost_commitments", "project_decisions",
                    "unresolved_risks")


def _lock_inputs(path: str | None) -> dict[str, Any]:
    """
    The --inputs file for pdr-lock and pdr-brief. A key this command does
    not know is refused: `providers` instead of `approved_providers` used
    to be dropped without a word, and the request then showed "none".
    """
    inputs = _load_json_arg(path) or {}
    if not isinstance(inputs, dict):
        raise _InputError("--inputs must be a JSON object")
    unknown = sorted(k for k in inputs if k not in _LOCK_INPUT_KEYS)
    if unknown:
        raise _InputError(
            f"--inputs has key(s) this command does not use: "
            f"{', '.join(one_line(k, 40) for k in unknown)}. "
            f"Allowed: {', '.join(_LOCK_INPUT_KEYS)}"
        )
    return inputs


def cmd_pdr_lock(args) -> int:
    inputs = _lock_inputs(args.inputs)
    conn = connect()
    try:
        if args.approval_request:
            if args.inputs or args.architecture_doc:
                print("Error: --approval-request replays what was approved; "
                      "do not pass --inputs or --architecture-doc with it")
                return 1
            result = pdr_queries.lock_architecture(
                conn, project_id=args.project_id,
                approval_request_id=args.approval_request,
            )
        else:
            result = pdr_queries.lock_architecture(
                conn, project_id=args.project_id,
                architecture_doc_path=_abs_path(args.architecture_doc),
                approved_providers=inputs.get("approved_providers"),
                cost_commitments=inputs.get("cost_commitments"),
                project_decisions=inputs.get("project_decisions"),
                unresolved_risks=inputs.get("unresolved_risks"),
            )
    finally:
        conn.close()
    rc = _gate_result(result, "Project not found")
    if rc is not None:
        return rc
    if args.json:
        _print_json(result)
    elif result.get("status") == "awaiting_approval":
        _print_block(result["summary_markdown"])
        print()
        print(f"Awaiting approval: request {result['approval_request_id']}")
        print(result["how_to_approve"])
        print(f"Then: cip-composer pdr-lock {args.project_id} "
              f"--approval-request {result['approval_request_id']}")
    else:
        if result.get("unchanged"):
            print("Nothing changed since the active lock.")
        print(f"Lock v{result['version']}: {result['architecture_lock_id']}")
        print(f"  Decisions: {result['decision_count']}")
        print(f"  Approved by: {result['approved_by']}")
        print(f"  Hash: {result['payload_hash'][:16]}...")
        if result.get("tasks_abandoned"):
            # Work the new lock does not include ends with it.
            print(f"  Tasks abandoned (not carried by this lock): "
                  f"{len(result['tasks_abandoned'])}")
            for tid in result["tasks_abandoned"]:
                print(f"    {one_line(tid, 60)}")
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
        p, pdr, needs = result["progress"], result["pdr"], result["needs"]
        print(f"Project: {one_line(result['project_name'], 120)}")
        if pdr["section_count"] is not None:
            print(f"  PDR sections mapped: {pdr['mapped_section_count']}/{pdr['section_count']}")
        print(f"  Requirements: {result['requirements']['total']} "
              f"({p['in_scope']} in scope)")
        print(f"  Needs:        {needs['decided']}/{needs['total']} decided, "
              f"registry answered {needs['registry_answered_pct']}%")
        print(f"  Decided:      {p['decided']}")
        print(f"  Locked:       {p['locked']}")
        print(f"  Tasked:       {p['tasked']}")
        print(f"  Implemented:  {p['implemented']}")
        print(f"  Verified:     {p['verified']} "
              f"({p['verified_cip_executed']} CIP-executed, "
              f"{p['verified_agent_reported_only']} agent-reported)")
        c = result["compliance"]
        if c["tasks_worked_without_lock_check"]:
            print(f"  Tasks worked with no lock check on record: "
                  f"{c['tasks_worked_without_lock_check']}")
        # What was actually run for each CIP-executed pass, and where. The
        # count above says a command exited 0; these lines say which
        # command, at which commit, in which repository.
        runs = [(r["slug"], e) for r in result["by_requirement"] for e in r["evidence"]
                if e.get("recorded_by") == "cip_executed" and e.get("outcome") == "pass"]
        if runs:
            print("  CIP-executed passes (check the command tests the criterion, and "
                  "that the repository is this project's):")
            for slug, e in runs:
                print(f"    {one_line(slug, 80)} #{e['criterion_ordinal']}: "
                      f"{one_line(e.get('command'), 200)}")
                print(f"      at {one_line(e.get('commit_sha'), 40)} in "
                      f"{one_line(e.get('evidence_ref'), 200) or 'repository not recorded'}"
                      + (" (working tree dirty)" if e.get("worktree_dirty") else ""))
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
    rc = _gate_result(result, "Capability need not found")
    if rc is not None:
        return rc
    if args.json:
        _print_json(result)
    else:
        # Every value below is stored text; each is printed as ONE line.
        v = one_line
        print(f"Need: {v(result['slug'])} — {v(result['description'])}")
        print(f"Queries: {', '.join(v(x) for x in result['queries'])}"
              + ("" if result["counts_as_mandatory_sweep"] else "  (extra search, not the sweep)"))
        print(f"Registry size: {result['registry_size']}  coverage: {result['coverage_outcome']}")
        print(f"Candidates evaluated: {result['candidate_count']}")
        for c in result["candidates"]:
            score = f"fit={c['fit_score']:.3f}"
            gaps = f"gaps={c['blocking_gap_count']}"
            intrinsic = (f"score={c['intrinsic_score']:.3f}"
                         if c["intrinsic_score"] is not None else "score=n/a")
            print(f"  {v(c['display_name'])} ({v(c['ecosystem'])}) — {score}, {gaps}, {intrinsic}")
        for u in result["unevaluable"]:
            print(f"  not selectable: {v(u['normalized_key'])} — {v(u['reason'])}")
        for m in result["evaluation_matches"]:
            print(f"  banked evaluation: {v(m['asset_slug'])} [{v(m['verdict'])}, "
                  f"{v(m['evidence_depth'])}, {v(m['match'])} match]")
    return 0


def cmd_record_decision(args) -> int:
    conn = connect()
    try:
        result = pdr_queries.record_decision(
            conn, req_id=args.req_id,
            verdict=args.verdict.upper() if args.verdict else None,
            chosen_capability_version_id=args.capability_version_id,
            rationale=args.rationale or "",
        )
    finally:
        conn.close()
    rc = _gate_result(result, "Capability need not found")
    if rc is not None:
        return rc
    if args.json:
        _print_json(result)
    else:
        print(f"Recorded: {result['verdict']} for {result['need']} "
              f"(decided by {result['decided_by']})")
        if result["flags"]:
            print(f"  flags: {', '.join(result['flags'])}")
    return 0


def cmd_check_against_lock(args) -> int:
    proposed = _load_json_arg(args.change) or {}
    if not isinstance(proposed, dict):
        print("Error: --change must be a JSON object")
        return 1
    if args.description:
        proposed["description"] = args.description
    if args.task:
        proposed["build_task_id"] = args.task
    if args.path:
        proposed["changed_paths"] = list(args.path)
    if args.provider:
        proposed["provider_changes"] = [{"name": p, "action": "add"} for p in args.provider]
    if args.dependency:
        deps = []
        for d in args.dependency:
            eco, _, name = d.partition(":")
            deps.append({"ecosystem": eco, "name": name})
        proposed["dependency_adds"] = deps
    if args.project_decision:
        changes = []
        for d in args.project_decision:
            key, _, choice = d.partition("=")
            changes.append({"key": key.strip(), "choice": choice.strip()})
        proposed["project_decision_changes"] = changes

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
        print(f"Verdict: {one_line(result['verdict'])}")
        for r in result["reasons"]:
            print(f"  - {one_line(r)}")
    return 0 if result["verdict"] == "allowed" else 2


def cmd_record_build_progress(args) -> int:
    conn = connect()
    try:
        if args.build_task_id:
            verification = _load_json_arg(args.verification)
            result = pdr_queries.record_build_progress(
                conn,
                build_task_id=args.build_task_id,
                status=args.status,
                target_commit=args.target_commit,
                verification=verification,
            )
        else:
            req_ids = [r.strip() for r in (args.req_ids or "").split(",") if r.strip()]
            result = pdr_queries.record_build_progress(
                conn,
                project_id=args.project_id,
                architecture_lock_id=args.lock_id,
                title=args.title,
                objective=args.objective,
                req_ids=req_ids,
                permitted_paths=list(args.permitted_path or []),
                reused_capability_version_id=args.reused_capability_version_id,
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
    inputs = _lock_inputs(args.inputs)
    conn = connect()
    try:
        result = pdr_queries.build_approval_brief(
            conn, project_id=args.project_id,
            architecture_doc_path=_abs_path(args.architecture_doc),
            approved_providers=inputs.get("approved_providers"),
            cost_commitments=inputs.get("cost_commitments"),
            project_decisions=inputs.get("project_decisions"),
            unresolved_risks=inputs.get("unresolved_risks"),
        )
    finally:
        conn.close()
    if result is None:
        print("Project not found")
        return 1
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json(result)
    else:
        _print_block(result["brief_markdown"])
        for u in result["unresolved"]:
            print(f"BLOCKER: {one_line(u, 400)}")
    return 0 if not result.get("unresolved") else 2


# ---------------------------------------------------------------------------
# Human approvals
#
# These are the only code paths that record a human decision. They demand an
# interactive terminal and a typed confirmation code, so an approval cannot
# happen as a side effect of an agent running a command. This is governed
# cooperation, not a sandbox: an agent with a shell that sets out to defeat
# it can. Do not ask an agent to run these.
# ---------------------------------------------------------------------------

def _human_identity() -> str:
    """
    Who is at the keyboard, as the operating system reports it.

    os.getlogin() asks the OS (the controlling terminal's login on POSIX,
    GetUserName on Windows). getpass.getuser() is only the fallback: it
    reads LOGNAME / USER / USERNAME first, which anything in the
    environment can set.
    """
    import getpass
    import os
    import socket
    user = None
    try:
        user = os.getlogin()
    except OSError:
        try:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            try:
                user = getpass.getuser()
            except Exception:
                user = None
    return f"{one_line(user or 'unknown', 80)}@{one_line(socket.gethostname(), 120)}"


def _require_interactive() -> str | None:
    """None if a human is at a terminal, else why not."""
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return (
            "Approvals must be given by a person at an interactive terminal. "
            "This session is not interactive, so nothing was recorded."
        )
    return None


def _ask(prompt: str) -> str | None:
    """
    One typed line, or None if there is nobody to type it. On Windows the
    NUL device reports itself as a terminal, so isatty() alone is not
    enough: a read that hits end-of-input is treated as "no human".
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _print_request(req: dict[str, Any]) -> None:
    """Everything a person should read before deciding a request."""
    _print_block(req["summary"])
    print()
    print(f"Request:  {req['id']}")
    print(f"Project:  {one_line(req['project_name'], 120)}")
    print(f"Kind:     {req['kind']}")
    print(f"Content:  {req['subject_hash'][:16]}")
    print(f"State:    {req['state']}"
          + (f" by {one_line(req['approver'], 120)}" if req.get("approver") else ""))
    if req.get("source_check"):
        print(f"PDR file: {one_line(req['source_check'], 400)}")
    if req.get("doc_check"):
        print(f"Architecture document: {one_line(req['doc_check'], 400)}")
    if req.get("obsolete_reason"):
        print(f"OBSOLETE: {one_line(req['obsolete_reason'], 400)}")


def cmd_approvals_list(args) -> int:
    conn = connect()
    try:
        rows = pdr_approvals.list_requests(
            conn, state=None if args.all else "pending",
        )
        for r in rows:
            r["obsolete_reason"] = pdr_queries.request_obsolete_reason(conn, r)
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No approval requests." if args.all else "Nothing is waiting for approval.")
        return 0
    _print_row_table(
        [{"id": r["id"][:8],
          "project_name": one_line(r["project_name"], 40),
          "kind": r["kind"],
          "state": r["state"] + (" (obsolete)" if r["obsolete_reason"] else ""),
          "requested_at": r["requested_at"][:16]} for r in rows],
        ["id", "project_name", "kind", "state", "requested_at"],
    )
    return 0


def cmd_approvals_show(args) -> int:
    conn = connect()
    try:
        req = pdr_queries.describe_request(conn, args.request_id)
    finally:
        conn.close()
    if req is None:
        print("Approval request not found (give at least 6 characters of its id; "
              "a prefix must match exactly one request).")
        return 1
    if args.json:
        _print_json(req)
        return 0
    _print_request(req)
    return 0


def _decide(args, outcome: str) -> int:
    problem = _require_interactive()
    if problem:
        print(problem)
        return 3
    conn = connect()
    try:
        req = pdr_queries.describe_request(conn, args.request_id)
        if req is None:
            print("Approval request not found (give at least 6 characters of its id; "
                  "a prefix must match exactly one request).")
            return 1
        if req["state"] != "pending":
            print(f"Already {req['state']} by {one_line(req['approver'], 120)}.")
            return 1
        _print_request(req)
        print()
        if req.get("obsolete_reason"):
            print("This request has been overtaken and cannot be decided. "
                  "Ask for a fresh one.")
            return 1
        code = req["subject_hash"][:6]
        verb = "APPROVE" if outcome == "approved" else "REJECT"
        print(f"You are about to {verb} this {req['kind'].replace('_', ' ')} "
              f"for project '{one_line(req['project_name'], 120)}'.")
        typed = _ask(f"Type the content code {code} to confirm: ")
        if typed is None:
            print("No input was available. Nothing was recorded.")
            return 3
        if typed != code:
            print("Code did not match. Nothing was recorded.")
            return 1
        note = args.note
        if outcome == "rejected" and not note:
            note = _ask("What needs to change? ")
        # The summary was on screen for as long as it took to read. Look
        # again before recording: record_approval checks the request's
        # place in history, not whether a decision or the scope moved under
        # it while the person was reading.
        conn.rollback()
        now = pdr_queries.describe_request(conn, req["id"])
        if now is None or now["state"] != "pending" or now.get("obsolete_reason"):
            why = (now or {}).get("obsolete_reason") or "it has already been decided"
            print(f"The project changed while this was on screen: {one_line(why, 400)}.")
            print("Nothing was recorded. Ask for a fresh request.")
            return 1
        result = pdr_approvals.record_approval(
            conn, request_id=req["id"], outcome=outcome,
            approver=_human_identity(), note=note,
        )
    finally:
        conn.close()
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    print(f"Recorded: {result['outcome']} by {result['approver']}.")
    return 0


def cmd_approvals_approve(args) -> int:
    return _decide(args, "approved")


def cmd_approvals_reject(args) -> int:
    return _decide(args, "rejected")


def _decode_output(data: bytes) -> str:
    """Test output for display only. Never fails: the digest is over the bytes."""
    return (data or b"").decode("utf-8", errors="replace")


def cmd_verify(args) -> int:
    """
    Run an acceptance test and record what CIP itself observed.

    The command runs in the current directory (the target repo), without
    a shell. CIP records the exit code, a digest of the raw output, the
    commit and whether the working tree was dirty, as a `cip_executed`
    verification.

    What that proves is narrow and worth saying plainly: this command
    exited 0 here, at this commit. It does not prove the command tests
    the criterion. A run on a dirty tree is recorded but does not count
    as CIP-executed in coverage, because the code that ran is not the
    code at the commit.
    """
    import hashlib
    import shutil
    import subprocess

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("Error: give the test command after --")
        return 1

    def git(*argv: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *argv], capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return _decode_output(out.stdout).strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if not commit:
        print("Error: not inside a git repository with a commit; "
              "a verification must name the commit it ran against.")
        return 1
    status = git("status", "--porcelain")
    if status is None:
        print("Error: could not read the working tree state from git.")
        return 1
    dirty = bool(status)

    # Resolve the program the way a shell would (PATHEXT on Windows, so a
    # .cmd / .bat shim such as npm or pytest is found) without using one.
    program = shutil.which(command[0])
    if program is None:
        print(f"Error: could not find the program '{one_line(command[0], 200)}' on PATH")
        return 1

    try:
        proc = subprocess.run(
            [program, *command[1:]], capture_output=True, timeout=args.timeout,
        )
        exit_code = proc.returncode
        raw = (proc.stdout or b"") + (proc.stderr or b"")
    except subprocess.TimeoutExpired:
        exit_code, raw = 124, f"timed out after {args.timeout}s".encode()
    except OSError as e:
        print(f"Error: could not run the command: {e}")
        return 1
    output = _decode_output(raw)

    # Look again. A command that edits tracked files (a formatter, a
    # snapshot update, a build step) leaves the tree dirty behind it, and
    # what it tested was then not the commit either.
    after = git("status", "--porcelain")
    if after is None or after:
        dirty = True
    repo_root = git("rev-parse", "--show-toplevel")

    verification = {
        "outcome": "pass" if exit_code == 0 else "fail",
        "commit_sha": commit,
        "test_ref": args.test_ref or " ".join(command),
        "command": " ".join(command),
        "exit_code": exit_code,
        "output_digest": hashlib.sha256(raw).hexdigest(),
        "worktree_dirty": dirty,
        # Which repository this ran in. The commit alone does not say.
        "evidence_ref": f"repo:{one_line(repo_root, 400)}" if repo_root else None,
    }
    if pdr_store.is_uuid(args.criterion):
        verification["criterion_id"] = args.criterion
    else:
        if not args.req or not (args.criterion.isascii() and args.criterion.isdigit()):
            print("Error: --criterion takes a criterion id, or an ordinal together with --req")
            return 1
        verification["req_id"] = args.req
        verification["criterion_ordinal"] = int(args.criterion)

    conn = connect()
    try:
        result = pdr_queries.record_executed_verification(
            conn, build_task_id=args.task, verification=verification,
        )
    finally:
        conn.close()
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    if args.json:
        _print_json({**result, "exit_code": exit_code, "commit_sha": commit,
                     "worktree_dirty": dirty, "repository": repo_root})
    else:
        tail = "\n".join(printable(output).strip().splitlines()[-15:])
        if tail:
            print(tail)
        print(f"{result['outcome'].upper()}: {one_line(result['requirement'])} "
              f"#{result['criterion_ordinal']} at {commit[:12]}"
              + (" (working tree dirty)" if dirty else "")
              + (f" in {one_line(repo_root, 200)}" if repo_root else ""))
        if dirty and exit_code == 0:
            print("  The working tree has uncommitted changes, so this pass is recorded "
                  "but does not count as CIP-executed. Commit and run it again.")
        for r in result.get("remaining", []):
            print(f"  still unverified: {one_line(r)}")
    return 0 if exit_code == 0 else 2


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
        # Everything below was written by a model. One line per value.
        v = one_line
        reqs = result.get("requirements", [])
        print(f"Drafted {len(reqs)} requirements "
              f"(via {v(result.get('provider_id', '?'))}/{v(result.get('model_id', '?'))})")
        for i, r in enumerate(reqs, 1):
            if not isinstance(r, dict):
                continue
            print(f"  {i}. [{v(r.get('priority', '?'))}] {v(r.get('slug', '?'))}: "
                  f"{v(r.get('text', '(no text)'))}")
        print("These are drafts. record_requirements checks every quote against the PDR.")
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
        print(f"Suggested verdict: {one_line(result.get('verdict', '?'))}")
        print(f"Rationale: {one_line(result.get('rationale', ''))}")
        if result.get("chosen_capability_version_id"):
            print(f"Chosen: {one_line(result['chosen_capability_version_id'])}")
        if result.get("note"):
            print(f"Note: {one_line(result['note'])}")
        print("A suggestion from a free model. record_decision computes CIP's own verdict.")
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
            if not isinstance(c, dict):
                continue
            ifaces = len(c.get("interfaces") or [])
            deps = len(c.get("dependencies") or [])
            print(f"  {one_line(c.get('name', '?'))} ({one_line(c.get('kind', '?'))}) "
                  f"— {ifaces} interfaces, {deps} deps")
    return 0


def cmd_fair_status(args) -> int:
    from integrations.fair_client import FairClient
    status = FairClient().status()
    if args.json:
        _print_json(status)
    else:
        print(f"FAIR: {'ONLINE' if status['available'] else 'OFFLINE'}")
        print(f"  Client: {one_line(status['client_id'])}")
        if status["error"]:
            print(f"  Reason: {one_line(status['error'], 400)}")
        for p in status["providers"]:
            models = ", ".join(one_line(m) for m in p.get("models") or [])
            print(f"  {one_line(p['provider_id'])}: {one_line(p['status'])} ({models})")
    return 0


def cmd_eval_record(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    missing = [k for k in ("asset_slug", "verdict") if not payload.get(k)]
    if missing:
        print(f"Error: JSON is missing required field(s): {', '.join(missing)}")
        return 1
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
            evidence_depth=payload.get("evidence_depth"),
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

    ps = subs.add_parser("pdr-status",
                          help="Where a PDR project is and what is allowed next.")
    ps.add_argument("project", help="Project UUID or name.")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_pdr_status)

    ip = subs.add_parser("ingest-pdr", help="Ingest a PDR document into a project.")
    ip.add_argument("file", help="Path to the PDR document (.md, .markdown, .txt, .rst).")
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

    rn = subs.add_parser("record-needs",
                          help="Record capability needs and link them to requirements.")
    rn.add_argument("project_id", help="UUID of the project.")
    rn.add_argument("file", help="JSON file with a needs array.")
    rn.add_argument("--json", action="store_true")
    rn.set_defaults(func=cmd_record_needs)

    cr = subs.add_parser("confirm-requirements",
                          help="Open (or report on) the requirements confirmation request.")
    cr.add_argument("project_id", help="UUID of the project.")
    cr.add_argument("--json", action="store_true")
    cr.set_defaults(func=cmd_confirm_requirements)

    pl = subs.add_parser("pdr-lock",
                          help="Request, or complete, an architecture lock. "
                               "Approval is given separately with `approvals approve`.")
    pl.add_argument("project_id", help="UUID of the project.")
    pl.add_argument("--architecture-doc", dest="architecture_doc", default=None,
                     help="Path to the architecture document.")
    pl.add_argument("--inputs", default=None,
                     help="JSON file with approved_providers, cost_commitments, "
                          "project_decisions, unresolved_risks.")
    pl.add_argument("--approval-request", dest="approval_request", default=None,
                     help="Write the lock from this approved request (second call). "
                          "Replays exactly what was approved.")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_pdr_lock)

    pc = subs.add_parser("pdr-coverage", help="PDR implementation coverage report.")
    pc.add_argument("project_id", help="UUID of the project.")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_pdr_coverage)

    pb = subs.add_parser("pdr-brief", help="Preview what the lock would freeze.")
    pb.add_argument("project_id", help="UUID of the project.")
    pb.add_argument("--architecture-doc", dest="architecture_doc", default=None)
    pb.add_argument("--inputs", default=None,
                     help="JSON file, same shape as for pdr-lock.")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=cmd_pdr_brief)

    sr = subs.add_parser("search-for-requirement",
                          help="Search registry and evaluation store for a capability need.")
    sr.add_argument("req_id", help="UUID of the capability need.")
    sr.add_argument("--query", default=None,
                     help="Extra query text. Omit it (and all filters) to run "
                          "the mandatory sweep of the need's own search terms.")
    sr.add_argument("--ecosystem", default=None)
    sr.add_argument("--capability-kind", dest="capability_kind", default=None)
    sr.add_argument("--kind", default=None, help="Filter by component_kind.")
    sr.add_argument("--runtime", default=None)
    sr.add_argument("--cost-tier", dest="cost_tier", default=None)
    sr.add_argument("--limit", type=int, default=20)
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=cmd_search_for_requirement)

    rd = subs.add_parser("record-decision",
                          help="Record the decision for a capability need. "
                               "Omit the verdict to take the one CIP computes.")
    rd.add_argument("req_id", help="UUID of the capability need.")
    rd.add_argument("verdict", nargs="?", default=None,
                     help="Proposed verdict: ADOPT, ADAPT, WRAP, REFERENCE or BUILD.")
    rd.add_argument("--capability-version-id", dest="capability_version_id", default=None,
                     help="Required for ADOPT/ADAPT/WRAP/REFERENCE.")
    rd.add_argument("--rationale", default=None, help="Free-text reason.")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(func=cmd_record_decision)

    ca_lock = subs.add_parser("check-against-lock",
                               help="Check a proposed change against the architecture lock.")
    ca_lock.add_argument("project_id", help="UUID of the project.")
    ca_lock.add_argument("--description", default=None, help="Description of the change.")
    ca_lock.add_argument("--task", default=None, help="Build task UUID (needed with --path).")
    ca_lock.add_argument("--path", action="append", default=None,
                          help="A repo-relative path the change touches. Repeatable.")
    ca_lock.add_argument("--provider", action="append", default=None,
                          help="An external provider the change would add. Repeatable.")
    ca_lock.add_argument("--dependency", action="append", default=None,
                          help="A dependency the change would add, as ecosystem:name. Repeatable.")
    ca_lock.add_argument("--project-decision", dest="project_decision", action="append",
                          default=None,
                          help="A cross-cutting decision the change would alter, "
                               "as key=choice. Repeatable.")
    ca_lock.add_argument("--change", default=None,
                          help="JSON file with a full proposed_change object.")
    ca_lock.add_argument("--json", action="store_true")
    ca_lock.set_defaults(func=cmd_check_against_lock)

    bp = subs.add_parser("record-build-progress",
                          help="Create or update a build task.")
    bp.add_argument("--build-task-id", dest="build_task_id", default=None,
                     help="UUID of existing task (update mode).")
    bp.add_argument("--project-id", dest="project_id", default=None,
                     help="UUID of project (create mode).")
    bp.add_argument("--lock-id", dest="lock_id", default=None,
                     help="UUID of the active architecture_lock (optional; create mode).")
    bp.add_argument("--title", default=None, help="Task title (create mode).")
    bp.add_argument("--objective", default=None, help="Task objective (create mode).")
    bp.add_argument("--req-ids", dest="req_ids", default=None,
                     help="Comma-separated requirement/need slugs or UUIDs (create mode).")
    bp.add_argument("--permitted-path", dest="permitted_path", action="append", default=None,
                     help="A path the task may touch (create mode). Repeatable; required.")
    bp.add_argument("--reused-capability-version-id", dest="reused_capability_version_id",
                     default=None, help="Locked capability version this task integrates.")
    bp.add_argument("--status", default=None,
                     help="New status (update mode): planned/in_progress/implemented/verified/abandoned.")
    bp.add_argument("--target-commit", dest="target_commit", default=None)
    bp.add_argument("--verification", default=None,
                     help="JSON file with a verification object or list (update mode).")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_record_build_progress)

    apr = subs.add_parser("approvals",
                           help="HUMAN ONLY: review and decide approval requests.")
    apr_subs = apr.add_subparsers(dest="approvals_cmd", required=True)
    apr_l = apr_subs.add_parser("list", help="Requests waiting for approval.")
    apr_l.add_argument("--all", action="store_true", help="Include decided requests.")
    apr_l.add_argument("--json", action="store_true")
    apr_l.set_defaults(func=cmd_approvals_list)
    apr_s = apr_subs.add_parser("show", help="Show exactly what is being approved.")
    apr_s.add_argument("request_id", help="Request UUID or a unique prefix.")
    apr_s.add_argument("--json", action="store_true")
    apr_s.set_defaults(func=cmd_approvals_show)
    apr_a = apr_subs.add_parser("approve", help="Approve a request (interactive terminal only).")
    apr_a.add_argument("request_id", help="Request UUID or a unique prefix.")
    apr_a.add_argument("--note", default=None)
    apr_a.set_defaults(func=cmd_approvals_approve)
    apr_r = apr_subs.add_parser("reject", help="Reject a request (interactive terminal only).")
    apr_r.add_argument("request_id", help="Request UUID or a unique prefix.")
    apr_r.add_argument("--note", default=None, help="What needs to change.")
    apr_r.set_defaults(func=cmd_approvals_reject)

    vf = subs.add_parser("verify",
                          help="Run an acceptance test here and record the result as CIP-executed.")
    vf.add_argument("--task", required=True, help="Build task UUID.")
    vf.add_argument("--criterion", required=True,
                     help="Acceptance criterion UUID, or its ordinal together with --req.")
    vf.add_argument("--req", default=None, help="Requirement slug or UUID (with an ordinal).")
    vf.add_argument("--test-ref", dest="test_ref", default=None,
                     help="Test identifier, e.g. tests/test_auth.py::test_login.")
    vf.add_argument("--timeout", type=int, default=600)
    vf.add_argument("--json", action="store_true")
    vf.add_argument("command", nargs=argparse.REMAINDER,
                     help="The test command, after --.")
    vf.set_defaults(func=cmd_verify)

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
    # A Windows console, or output piped through one, is often cp1252.
    # Printing a character it cannot encode must not crash the command.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = _build_parser()
    args = parser.parse_args(argv)
    import contextlib
    try:
        to_terminal = sys.stdout.isatty()
    except (AttributeError, ValueError):
        to_terminal = False
    wrap = to_terminal or getattr(args.func, "__name__", "") in _ALWAYS_WRAPPED
    safe = _SafeStream(sys.stdout, wrap=wrap)
    with contextlib.redirect_stdout(safe):
        try:
            return args.func(args)
        except _InputError as e:
            print(f"Error: {e}")
            return 1
        finally:
            safe.flush()


if __name__ == "__main__":
    sys.exit(main())
