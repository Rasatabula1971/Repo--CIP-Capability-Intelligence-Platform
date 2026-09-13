# CIP v2 Phase 0 Audit Report

**Date:** 2026-09-10
**Auditor:** Claude Opus 4.6
**Codebase:** Repo--CIP-Capability-Intelligence-Platform @ `7f6ebe6`
**Method:** Source-level review of every module, migration, and test file

---

## Capability Matrix

| # | v2 Capability | Status | Coverage |
|---|---|---|---|
| 1 | Broader Discovery | **PARTIAL** | 60% |
| 2 | Code-Level Inspection | **PARTIAL** | 45% |
| 3 | Capability Extraction | **PARTIAL** | 30% |
| 4 | Symbol-Level Reuse | **MISSING** | 0% |
| 5 | Dependency Intelligence | **PARTIAL** | 35% |
| 6 | Executable Compatibility Tests | **MISSING** | 0% |
| 7 | Adapter Generation | **PARTIAL** | 20% |
| 8 | Verification Lifecycle | **PARTIAL** | 50% |
| 9 | Project Experience Memory | **MISSING** | 0% |
| 10 | Failure-Driven Replacement | **MISSING** | 0% |
| 11 | Requirement Traceability | **PARTIAL** | 55% |
| 12 | Build-Gap Proof | **EXISTS** | 85% |

**Summary:** 1 EXISTS, 6 PARTIAL, 5 MISSING

---

## Detailed Findings

### Capability 1: Broader Discovery — PARTIAL (60%)

**What exists:**
- `connectors/base.py` — Clean `SourceConnector` protocol with 4 obligations (enumerate, current_revision, availability, snapshot). Provider-agnostic value types (`AssetRef`, `RevisionKey`, `FileEntry`). Connector registry.
- `connectors/github/client.py` — Full GitHub connector (search, revision, availability, tarball snapshot). Rate-limit and error classification.
- `scripts/ingest/github_search.py` — GitHub repository ingester with cursor tracking.
- `scripts/ingest/awesome_list.py` — Extracts GitHub links from awesome-list READMEs.
- `scripts/ingest/mcp_registry.py` — Local `~/.claude.json` MCP server ingester.
- `scripts/ingest/claude_skills.py` — Local Claude skill/plugin scanner.
- `scripts/ingest/_base.py` — Shared ingestion plumbing: run tracking, provenance upserts, idempotent `upsert_component`.
- 448 components currently ingested (400 repos, 30 skills, 14 libraries, 2 MCP tools, 1 agent, 1 workflow template).

**What's missing for v2:**
- **No PyPI connector** — Python packages are only discovered through GitHub repos, not the PyPI registry directly.
- **No npm connector** — Node packages not discoverable.
- **No Hugging Face connector** — Models/datasets not searchable.
- **No Docker/OCI connector** — Container metadata not indexed.
- **No remote MCP registry connector** — Only reads local `~/.claude.json`, not public MCP registries.
- **Search is `ILIKE '%term%'` substring match** (`mcp_server/queries.py`) — no trigram, no relevance ranking, no semantic search.

**Reusable primitives:**
- The `SourceConnector` protocol and `_base.py` ingestion helpers are solid and ready for new connectors. Adding PyPI/npm means implementing `enumerate` + `current_revision` + `availability` + `snapshot` against their APIs — the pattern is established.
- The `ConnectorRegistry` is ready.

**Gap priority:** HIGH — search quality and PyPI/npm connectors are prerequisites for almost everything else.

---

### Capability 2: Code-Level Inspection — PARTIAL (45%)

**What exists:**
- `analysis/extractors/manifests.py` — Parses `pyproject.toml` (PEP 621), `requirements*.txt`, `package.json`. Extracts dependencies with evidence linking. Flags `setup.py` presence without executing it.
- `analysis/extractors/licenses.py` — Identifies LICENSE files, pattern-matches 12 SPDX signatures (MIT, Apache-2.0, BSD, GPL, LGPL, MPL, ISC, Unlicense) with high/low/unknown confidence.
- `analysis/extractors/interfaces.py` — Python AST parser: extracts public top-level functions and classes with signatures, return types, base classes, and public method names. Evidence-linked to file ranges.
- `analysis/extractors/secrets.py` — Secret pattern detection.
- `analysis/extractors/tests.py` — Test file detection.
- `analysis/evidence.py` — Evidence item model with locator types (`file_range`, `manifest_key`, `whole_file`).

**What's missing for v2:**
- **No `inspection_run` tracking** — inspections aren't recorded as auditable runs with parser versions and revisions.
- **No Node/TypeScript interface extraction** — `interfaces.py` is Python-only.
- **No release/tag inspection** — GitHub releases, changelogs not parsed.
- **No issue signal extraction** — open issue counts, bug labels not captured.
- **No example/test quality analysis** — test presence is detected but test coverage/quality is not assessed.
- **setup.py not parsed** — deliberately, for security. Gap documented in `manifests.py:166`.

**Reusable primitives:**
- The `EvidenceItem` model and `SourceFile` abstraction are ready for new extractors.
- The Python AST parser in `interfaces.py` is the foundation for symbol extraction (Capability 4).
- Manifest parsing covers the two most important ecosystems already.

**Gap priority:** MEDIUM — the extractors work, but lack auditability (`inspection_run`) and Node coverage.

---

### Capability 3: Capability Extraction — PARTIAL (30%)

**What exists:**
- `capability` table is the central identity (normalized_key, component_kind, capability_kind, runtime, cost_tier, license_spdx, metadata JSONB).
- `capability_version` → `capability_interface` (with `input_type`/`output_type` JSONB).
- `capability_dependency` links versions to declared dependencies.
- The interface extractor (`interfaces.py`) produces function/class-level evidence.
- `core/capability/normalize.py` and `core/capability/registry.py` handle identity normalization.

**What's missing for v2:**
- **Extraction is whole-repo granularity** — a `capability` row represents an entire repo or package. There is no mechanism to represent "function X from package Y" as a reusable unit.
- **No `capability_symbol` table** — the interface evidence exists in `evidence_item` but is not indexed into a queryable symbol table.
- **No sub-component extraction** — can't identify that a repo contains 3 independent tools and represent each separately.

**Reusable primitives:**
- The interface extractor already produces the raw data (function names, signatures, classes). What's missing is the schema to store and query it as first-class entities.

**Gap priority:** MEDIUM — the evidence pipeline exists; the storage/query layer for symbols does not.

---

### Capability 4: Symbol-Level Reuse — MISSING (0%)

**What exists:**
- Nothing. No `capability_symbol` table, no `symbol_interface` table, no `search_symbols` operation.

**What can be built on:**
- `analysis/extractors/interfaces.py` already extracts public Python functions/classes with full signatures. This is the raw data source.
- The evidence chain (`evidence_item` → `file_range` locator) already links interface evidence to specific source lines.

**Gap priority:** HIGH for v2 value — this is what makes CIP find functions, not just repos.

---

### Capability 5: Dependency Intelligence — PARTIAL (35%)

**What exists:**
- `analysis/extractors/manifests.py` extracts declared dependencies from `pyproject.toml`, `requirements.txt`, `package.json` with ecosystem/name/version_spec/kind.
- `capability_dependency` table links versions to dependencies.
- `core/policy/constraints.py` has `license_allowlist`, `license_denylist`, `runtime_allowlist`, `component_kind_allowlist` checks — but these are project-level filters, not dependency-resolution checks.

**What's missing for v2:**
- **No `dependency_fact` table** — dependencies are stored as evidence items but not as a normalized, queryable table with conflict detection.
- **No `environment_profile`** — no way to represent "this project runs on Python 3.11, Ubuntu 22.04, no GPU."
- **No `dependency_fit` operation** — can't answer "will package X install in environment Y?"
- **No version conflict detection** — can't detect that two pipeline components require incompatible versions of the same package.
- **No runtime/OS/GPU requirement resolution** — the constraint system checks `requires_gpu` in metadata but doesn't resolve actual system requirements from manifests.

**Reusable primitives:**
- The manifest extractor produces the raw dependency data with evidence linking.
- The constraint evaluation framework (`core/policy/constraints.py`) is extensible — adding new constraint kinds requires one function and one dict entry.

**Gap priority:** HIGH — dependency conflicts are the #1 reason integrations fail.

---

### Capability 6: Executable Compatibility Tests — MISSING (0%)

**What exists:**
- `core/policy/compatibility.py` — Static runtime compatibility matrix (`NATIVE_INTEROP`, `BRIDGEABLE`). Produces `compatible`/`adapter_needed`/`incompatible` verdicts with I/O type checking. Pure function, no DB, no LLM.
- This is the "first pass" static check mentioned in the v2 plan.

**What's missing for v2:**
- **No `verification_run` table** — no schema for recording test executions.
- **No `verification_assertion` table** — no schema for individual test assertions.
- **No isolated test runner** — no subprocess/venv/container execution environment.
- **No `verify_capability` operation** — can't prove a component installs/builds/imports.
- **No `verify_chain` operation** — can't prove two components work together.
- **No artifact capture** — no mechanism to record logs, exit codes, durations.

**Reusable primitives:**
- The static compatibility checker is the fast pre-filter that decides what's worth verifying. It works and is well-tested.

**Gap priority:** CRITICAL — this is the core v2 differentiator (moving from "plausible" to "verified").

---

### Capability 7: Adapter Generation — PARTIAL (20%)

**What exists:**
- `core/policy/compatibility.py` — Detects `adapter_needed` edges and provides `adapter_hint` strings for each bridgeable runtime pair (e.g., "subprocess.run() call, capture stdout", "httpx.Client call", "mcp.ClientSession + stdio_client"). 14 specific bridge hints defined.
- The `BRIDGEABLE` set defines which runtime pairs can be adapted.

**What's missing for v2:**
- **No `adapter_spec` table** — no schema for recording interface mismatches and transformation contracts.
- **No adapter skeleton generation** — hints are text strings, not code templates.
- **No adapter test generation** — no generated contract tests for adapters.
- **No LLM-assisted adapter code** — no bounded code generation.

**Reusable primitives:**
- The bridge hints in `_bridge_hint()` are the seed data for adapter templates. The runtime pair detection is solid.

**Gap priority:** MEDIUM — depends on verification lab (Capability 6) being built first.

---

### Capability 8: Verification Lifecycle — PARTIAL (50%)

**What exists:**
- `core/capability/lifecycle.py` — Full state machine with states: `CANDIDATE` → `ANALYZED` → `VERIFIED` → `CATALOGED`, plus `STALE`, `QUARANTINED`, `DEPRECATED`, `REVOKED`.
- Forward transitions enforced: `_FORWARD_TRANSITIONS` dict, `advance_to()` function.
- 7 publication guards checked before `CATALOGED`: current_revision, required_scorecard, confidence_threshold, no_blocking_gate, source_binding, summary, interfaces.
- `GuardsFailed` exception carries all failure reasons (not just first).
- State transitions audited via workflow engine (`state_transition` table, append-only).
- Migration `008_lifecycle.sql` adds `lifecycle_state` column with CHECK constraint.

**What's missing for v2:**
- **No `LICENSE_CHECKED` state** — the v2 plan inserts this between `DISCOVERED` and `INSPECTED`. Current states don't have it.
- **No `INSPECTED` state** — the current machine uses `CANDIDATE` → `ANALYZED` which conflates inspection with analysis.
- **No `SHORTLISTED`, `TEST_QUEUED`, `TESTING` states** — the verification sub-lifecycle doesn't exist.
- **No `USED`, `PROVEN` states** — no project-usage tracking in the lifecycle.
- **No `BLOCKED_FROM_REUSE` state** — license blocking uses the gate system, not the lifecycle state machine.
- **No `NEEDS_REVIEW` state** — uncertain license status has no lifecycle representation.

**Reusable primitives:**
- The state machine infrastructure is solid: `advance_to()`, guard system, audit trail via `state_transition`. Adding new states means extending the CHECK constraint (new migration), adding entries to `_FORWARD_TRANSITIONS`, and optionally new guards.
- The guard pattern (pure function returning `GuardFailure | None`) is clean and extensible.

**Gap priority:** MEDIUM — the machine works; it needs more states, not a different design.

---

### Capability 9: Project Experience Memory — MISSING (0%)

**What exists:**
- Nothing. No `component_project_use` table, no `failure_event` table, no `experience_score` table.

**What can be built on:**
- The `project` table exists.
- The `recommendation` and `recommendation_candidate` tables track what was recommended, but not what happened after.
- The scoring system (`scripts/score/component_scores.py`) could incorporate experience scores as a new dimension.

**Gap priority:** LOW initially — this becomes valuable only after verification (Capability 6) is producing data.

---

### Capability 10: Failure-Driven Replacement — MISSING (0%)

**What exists:**
- Nothing. No failure diagnosis, no repair loop, no automatic alternative search on integration failure.

**What can be built on:**
- The `suggest_pipeline` composition already handles "stages with zero candidates get a gap note" — the concept of "no match" is present.
- The recommendation system could be re-invoked with exclusions when a component fails verification.

**Gap priority:** LOW initially — depends on Capabilities 6 and 9 existing first.

---

### Capability 11: Requirement Traceability — PARTIAL (55%)

**What exists:**
- `project` table — project identity.
- `project_requirement` table — requirements with slug and description, scoped to a project.
- `requirement_constraint` table — typed constraints on requirements (required_interface, license_allowlist, forbidden_dependency).
- `fit_evaluation` table — per (requirement, capability_version) fit score with deterministic `computed_hash`.
- `fit_gap` table — specific mismatches (blocking or not).
- `fit_evidence_link` table — every fit claim traced to evidence items.
- `recommendation` table — verdict per requirement (ADOPT/ADAPT/WRAP/REFERENCE/REJECT/BUILD) with pinned revision.
- `recommendation_candidate` table — all candidates evaluated, ranked.
- `core/project/fit.py` — Pure fit evaluation function with 3 constraint matchers.
- `core/project/rules.py` — Declarative rules engine (YAML profiles, first-match, stable hashing).
- `core/project/types.py` — Full type system: Requirement, Constraint, CandidateInputs, FitResult, Verdict, Decision, ScoredCandidate.

**What's missing for v2:**
- **No `project_requirement_map` table** — the v2 plan wants requirement → capability → component/build gap → test in one view. The current schema has this data across multiple tables but no materialized mapping.
- **No requirement input format** — requirements must be inserted via SQL or code. No parser for structured requirement files (YAML/JSON).
- **No requirement coverage report** — no query or view that shows "X% of requirements have a recommendation."
- **No link to verification tests** — requirements map to recommendations but not to verification assertions.

**Reusable primitives:**
- The requirement → fit → recommendation chain is fully built and tested. The data model is sound.
- The rules engine is declarative and versioned — exactly what the v2 plan asks for.
- The fit evaluation is deterministic with hash verification — a strong foundation.

**Gap priority:** MEDIUM — the hard parts (fit evaluation, rules engine, recommendation pipeline) exist. What's missing is the last-mile UX (input format, coverage report, test linkage).

---

### Capability 12: Build-Gap Proof — EXISTS (85%)

**What exists:**
- `core/policy/build_gate.py` — Full search-before-build gate. `authorize_build()` refuses without search evidence. `can_generate_for()` returns allowed/refused with specific reasons.
- `build_authorization` table (migration 010) — append-only audit trail with actor/reason. UPDATE rule prevents silent rewrites. FK CASCADE deletes authorization when recommendation is replaced.
- Three refusal modes: `RecommendationNotFound`, `NotABuildVerdict`, `MissingSearchEvidence`.
- Documented-search check: requires `recommendation_candidate` rows OR `__no_candidates__` sentinel.
- 512 tests including dedicated build-gate tests (`test_step13_build_gate.py`).

**What's missing for v2:**
- **No UI/CLI surface for `authorize_build`** — the gate logic exists in `core/policy/build_gate.py` but isn't exposed as an MCP tool or CLI subcommand.
- **No coverage report** — can't query "how many requirements have authorized BUILD decisions."

**Reusable primitives:**
- This is the most complete v2 capability. The enforcement is code-level (not convention), the audit trail is append-only, and the search-evidence requirement is schema-enforced.

**Gap priority:** LOW — needs exposure through adapters (MCP/CLI/skill), not more logic.

---

## Cross-Cutting Gaps

### License Enforcement
- **Current:** License extractor identifies SPDX ids. `LicenseCompatibilityGate` in `core/policy/gates.py` blocks unknown/non-permissive licenses. Project constraints include `license_allowlist` and `license_denylist`.
- **Missing:** No `LICENSE_CHECKED` lifecycle state. No `license_status` enum (verified_open_source / needs_review / blocked / unknown). No `BLOCKED_FROM_REUSE` state. No file/subdirectory license-scope handling. No SBOM generation. No `NEEDS_REVIEW` status.
- **Assessment:** The gates enforce at publication time but not at selection time. v2 needs license gates earlier in the lifecycle.

### Search Quality
- **Current:** `ILIKE '%term%'` substring match in `mcp_server/queries.py`.
- **Missing:** Trigram indexing, TF-IDF, relevance ranking, tag/topic search, semantic search.
- **Assessment:** Every v2 capability depends on finding the right candidates. This is the highest-leverage quick win.

### Observability
- **Current:** `ingest_run` tracking with counts and status. LLM judgment rows record prompt versions and model names.
- **Missing:** No structured logging with correlation IDs. No duration/success-rate tracking for operations beyond ingestion. No system health summary.

### LLM Cost Management
- **Current:** `core/judgment/providers.py` abstraction with Gemini as primary. Model hardcoded to `gemini-3.5-flash-lite`.
- **Missing:** No per-operation token budgets. No graceful degradation. No provider fallback chain. No cost tracking.

---

## Gap-Ranked Implementation Backlog

Priority ordered by dependency chain and value delivery:

| Rank | Item | Blocks | Effort |
|---|---|---|---|
| 1 | Search quality (trigram + relevance ranking) | Everything downstream | S (3 days) |
| 2 | License lifecycle states + enforcement gate | Capabilities 3,6,7,8 | M (1 week) |
| 3 | `inspection_run` tracking table | Capability 2 auditability | S (2 days) |
| 4 | PyPI connector | Capabilities 1,3,4,5 | M (1 week) |
| 5 | npm connector | Capabilities 1,3,4 | M (1 week) |
| 6 | `dependency_fact` + `environment_profile` tables | Capability 5 | M (1 week) |
| 7 | `dependency_fit` operation | Capability 5 | M (1 week) |
| 8 | `capability_symbol` + `symbol_interface` tables | Capability 4 | M (1 week) |
| 9 | Python symbol indexer (from existing AST parser) | Capability 4 | M (1 week) |
| 10 | `search_symbols` operation | Capability 4 | S (3 days) |
| 11 | `verification_run` + `verification_assertion` tables | Capability 6 | M (1 week) |
| 12 | Subprocess + venv verification runner | Capability 6 | L (2 weeks) |
| 13 | `verify_capability` operation | Capability 6 | M (1 week) |
| 14 | Extended lifecycle states (LICENSE_CHECKED, INSPECTED, etc.) | Capability 8 | M (1 week) |
| 15 | Requirement input format (YAML parser) | Capability 11 | S (3 days) |
| 16 | Requirement coverage report | Capability 11 | S (2 days) |
| 17 | `adapter_spec` table + skeleton generation | Capability 7 | L (2 weeks) |
| 18 | `component_project_use` + `experience_score` tables | Capability 9 | M (1 week) |
| 19 | `failure_event` table + diagnose/replace loop | Capability 10 | L (2 weeks) |
| 20 | v2 operations exposed across all adapters (MCP/CLI/skill) | All capabilities | M (1 week) |

**Size key:** S = 1-3 days, M = 1 week, L = 2+ weeks

---

## Existing Test Coverage

- 512 tests passing across 30 test files
- Tests cover: provenance chain, workflow engine, connectors, ingestion, analysis/extraction, classification, identity linking, supersession detection, scoring, constraints, compatibility, composition (suggest + scaffold), CLI, MCP server, lifecycle, gates, build gate, recommendations
- Benchmark projects: not yet defined (Phase 0 deliverable)

---

## Architecture Decisions to Preserve (ADRs)

1. **Core domain logic has no HTTP/framework imports** — enforced by `import-linter`. Keep this.
2. **Scoring is deterministic, no LLM** — `core/scoring/` cannot import any provider client. Keep this.
3. **Append-only audit trail** — `state_transition`, `evidence_item` are append-only at DB level. Keep this.
4. **Fetching is not executing** — `connectors/github/client.py` uses tarball, not git clone. Keep this.
5. **LLM judgments never set deterministic scores** — judgment records influence, scoring computes. Keep this.
6. **Connectors are provider-agnostic** — the `SourceConnector` protocol has no provider-specific types. Keep this.
7. **Idempotent ingestion** — `_base.py` ensures re-running produces the same state. Keep this.
8. **Every migration is checksum-tracked** — editing an applied migration raises at runtime. Keep this.
