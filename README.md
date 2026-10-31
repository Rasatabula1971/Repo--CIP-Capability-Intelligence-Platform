# Capability Intelligence Platform (CIP)

CIP stops an AI coding agent from writing code that already exists.

It keeps a registry of reusable capabilities (libraries, repositories, agents, Claude skills, MCP tools and workflow templates), each tied to an exact source revision and scored from evidence read out of that source. Before anything is built, the agent searches the registry, and CIP records what was found, what was decided, and on what evidence. The unit of value is the capability, not the repository.

It ships as one Python package:

- a Postgres registry and an evaluation store;
- `cip-mcp`, an MCP server that gives an LLM the registry as tools;
- `cip-composer`, a CLI for people, including the only way to approve anything;
- a Claude plugin that bundles the MCP server with two skills.

Version 0.3.0, beta, one maintainer. New here? Read [`docs/WHAT_IS_CIP.md`](docs/WHAT_IS_CIP.md) for the plain-language version, or [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the technical one.

## What it is for

| Job | Where |
|-----|-------|
| Find out whether something already exists | `cip-composer find`; MCP `search_capabilities`, `browse_components` |
| Check a candidate against your project's rules | license, constraint fit, dependency fit, version conflicts, stage compatibility |
| Replace a dependency that broke | `cip-composer find-replacement`; MCP `find_replacement` |
| Map a codebase you already have | `cip-composer analyze-local`. No database needed |
| Compose a pipeline from existing parts | `cip-composer suggest`, `scaffold`, `flow` |
| Keep evaluations of repos and tools, and find them again by the problem they solve | [the evaluation store](#evaluation-store) |
| Hand an LLM a PDR and have it build the app with CIP as the system of record | [PDR-to-Build](#building-an-app-from-a-pdr) |

## What it does not do

Read this before relying on anything CIP tells you. The full list is [spec §12](docs/pdr_to_build_spec_v1_1.md#12-what-this-does-and-does-not-enforce).

- **The architecture lock is governed cooperation, not a sandbox.** It binds because the agent's workflow routes through CIP's tools. An agent with a shell can write code outside them. Nothing compares the target repository against the lock: drift detection is not built.
- **Agent-reported verification is self-reported.** `cip-composer verify` proves one thing: that one command exited 0 at one clean commit. Whether that command tests the criterion is for a person to judge.
- **CIP stands behind a verdict only where it holds the evidence.** Most of the registry was ingested from metadata and has not been through source analysis (Step 6), so most reuse decisions today are labelled LLM judgment, not CIP's. `pdr-coverage` shows the split.
- **An approval summary is a careful clerk's work, not proof.** The two human gates have been attacked by four independent reviews, and since the second nothing has obtained a lock or an approval a human did not give. The fixes from the fourth review have been tested but not reviewed by anyone but their author.
- **Not verified on Windows:** `verify` on a `.cmd` or `.bat` shim such as `npm`, which Windows runs through `cmd.exe`.
- **No CI.** The test suite needs a live Postgres and is run by hand.

## Requirements

- Python 3.11 or later (3.12 or later for the optional FAIR extra)
- PostgreSQL 14 or later, with two databases: `cip_local` and `cip_test`
- Optional: `GITHUB_TOKEN`, `GEMINI_API_KEY`, FAIR provider keys (see [Configuration](#configuration))

## Install and run the tests

**macOS / Linux (bash):**

```bash
cp .env.example .env          # set DATABASE_URL and TEST_DATABASE_URL
pip install -e ".[dev]"       # cip-mcp, cip-composer, cip-bootstrap, plus pytest and import-linter
export PYTHONPATH=.
cip-bootstrap                 # creates cip_local and cip_test if missing, migrates both
pytest
lint-imports                  # enforces the two structural rules
```

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env   # set DATABASE_URL and TEST_DATABASE_URL
pip install -e ".[dev]"
$env:PYTHONPATH = "."
cip-bootstrap
pytest
lint-imports
```

Notes:

- `cip-bootstrap` needs permission to create databases. If the `cip` role lacks it, pass `--admin-url postgresql://postgres@localhost:5432/postgres`, or create the two databases yourself and run `cip-bootstrap --skip-create`. `cip-bootstrap --verify` checks without writing. Exit code 2 means Postgres is unreachable; 3 means it cannot create a database that does not exist.
- Without the bootstrap: `python -m db.migrate up` migrates `DATABASE_URL`, `python -m db.migrate up --test` migrates `TEST_DATABASE_URL`, and `python -m db.migrate status` lists what is applied. The test suite also migrates the test database when it starts.
- The Windows Postgres installer sometimes takes port `5433` when `5432` is in use. Check with `netstat -ano | findstr LISTEN` and set the port in `.env`.
- The suite is about 1,770 test functions against a real Postgres. There is no CI: run it before merging.

### Upgrading a database that already has CIP on it

`cip-bootstrap` or `python -m db.migrate up` applies whatever migrations are new.

**Migration 030 is the exception.** `030_pdr_gap_closure.sql` was edited in place while the PDR-to-Build work was under review, before it was released. The runner stores a checksum for every applied file, so a database that applied an earlier draft of 030 stops with:

```
RuntimeError: Migration 030_pdr_gap_closure.sql was applied with a different checksum. ...
```

There are no down migrations. The fix is to drop and recreate the databases, and that deletes the registry and the evaluation store:

1. Export your evaluations: `cip-composer eval-search --limit 500 --json > evaluations.json`
2. Drop `cip_local` and `cip_test` as a role allowed to (for example `dropdb -U postgres cip_local`).
3. Run `cip-bootstrap`.
4. Re-run the ingesters ([`docs/ingestion_cadence.md`](docs/ingestion_cadence.md)). `python -m scripts.seed_evaluations` restores the evaluations bundled with the repo; `cip-composer eval-record <file.json>` restores your own, one evaluation per file.

A database that never applied 030 takes it like any other migration. 030 is on `main` now: every further schema change is a new migration.

## Configuration

Secrets go in environment variables. Identity, tier, quota and retention posture of model providers live in [`config/providers.yaml`](config/providers.yaml). If an agent session can read the repo folder, set API keys as user environment variables rather than in `.env`.

| Variable | Needed for |
|----------|------------|
| `DATABASE_URL`, `TEST_DATABASE_URL` | everything that touches the registry, and the tests |
| `GITHUB_TOKEN` | optional. Public reads work without it; a token raises GitHub's rate limit |
| `GEMINI_API_KEY` | model analysis (Step 7), enrichment, classification, cross-ecosystem linking, supersession detection, and `suggest` / `flow`. FAIR can also use it, see [FAIR](#fair-optional-free-model-routing) |
| `MODEL_PROVIDERS_CONFIG` | path to `providers.yaml` (default `./config/providers.yaml`) |
| `BUDGET_MODEL_CALLS_PER_RUN`, `BUDGET_MODEL_CALLS_PER_MONTH`, `BUDGET_WALLCLOCK_MINUTES_PER_RUN`, `BUDGET_SANDBOX_MINUTES_PER_RUN` | budget ceilings. The scheduler refuses to start a run if they are unset; `.env.example` has defaults |
| `CIP_DOCUMENT_ROOTS` | recommended. A path list confining where the server may read PDR and architecture documents. Set and blank means nothing can be read |
| FAIR variables | see [FAIR](#fair-optional-free-model-routing) |

## Using it

### From an LLM: the MCP server

```bash
claude mcp add cip -- cip-mcp
```

Or register it for every project at once with `setup-global-mcp.sh` / `setup-global-mcp.bat`, which install the package, create `.env`, ask for `DATABASE_URL` and register CIP at user scope. On Windows, `run-cip-mcp.bat` changes into `C:\CIP-Capability-Intelligence-Platform` before starting the server; edit that path if your clone is elsewhere.

Each tool's docstring in [`mcp_server/server.py`](mcp_server/server.py) is its contract. The tools by area:

| Area | Tools |
|------|-------|
| Search and detail | `search_capabilities`, `browse_components`, `capability_detail`, `search_symbols`, `ingest_symbols` |
| Fit and compatibility | `capability_constraint_fit`, `capability_license_check`, `capability_compatibility`, `dependency_fit`, `dependency_conflicts`, `create_adapter_spec`, `get_adapter_spec`, `list_adapter_specs` |
| Health and history | `verification_status`, `inspection_history`, `experience_summary`, `find_replacement` |
| Local analysis | `analyze_local`, `list_extractors` |
| Composition | `suggest_pipeline`, `scaffold_pipeline` |
| Evaluation store | `record_evaluation`, `search_evaluations`, `get_evaluation` |
| PDR-to-Build | `pdr_status`, `ingest_pdr`, `record_requirements`, `record_capability_needs`, `retire_requirement`, `request_requirements_confirmation`, `search_for_requirement`, `record_decision`, `pdr_approval_brief`, `lock_architecture`, `check_against_lock`, `record_build_progress`, `pdr_coverage` |
| FAIR | `fair_status`, `fair_extract_requirements`, `fair_suggest_verdict`, `fair_analyze_source` |
| Requirements and build gate (Steps 12–13) | `import_requirements`, `requirement_coverage`, `authorize_build`, `build_gate_check`, `build_coverage` |

`import_requirements` is refused for a project that has a PDR, and `authorize_build` refuses a capability need: the PDR workflow has its own path for both. No tool records a human approval.

### From the terminal: `cip-composer`

```bash
cip-composer info                                        # registry stats
cip-composer find "http client" --runtime python_import --cost-tier free
cip-composer find "video editing" --kind repo --tags video,ai --limit 10
cip-composer browse --kind mcp_tool --project-id <project-id>   # the project's constraints as hard filters
cip-composer analyze-local ./my-codebase                 # scans files only; no database
cip-composer suggest "transcribe audio then subtitle it" --out proposal.json
cip-composer scaffold --proposal proposal.json --out-dir ./my-pipeline
cip-composer flow "transcribe audio then subtitle it" --out-dir ./my-pipeline   # suggest + scaffold
cip-composer find-replacement <project-id> <capability-id> --failure-kind runtime_error --summary "what broke"
cip-composer dep-fit <capability-id> --project-id <project-id>
cip-composer dep-conflicts <capability-a> <capability-b>
```

`find` and `browse` filter by `--kind` (library, repo, agent, skill, mcp_tool, workflow_template), `--ecosystem`, `--runtime`, `--cost-tier`, `--license-status` and `--project-id`. `cip-composer --help` lists every command. `suggest` and `flow` need `GEMINI_API_KEY`.

### Evaluation store

One place for every evaluation of a repo, skill, agent, MCP server, prompt or workflow, so nothing is evaluated twice and good code is kept even when nothing in flight needs it. Each evaluation records:

- a verdict: Adopt, Adapt, Wrap, Reference, Reject or Build, the same six CIP's recommendations use;
- whether it is **banked**, kept for a future build. Verdict and banking are separate: a Reject can be banked;
- `evidence_depth`: `code_read`, `docs_only` or `metadata_only`. A code-quality grade is refused without `code_read`;
- `problem_types`, the tags it is recalled by (`oauth-flow`, `retry-with-backoff`, ...), and banked references to specific files.

```bash
cip-composer eval-record evaluation.json                 # one evaluation per file; upserts by asset_slug
cip-composer eval-search --problem-types oauth-flow,rate-limiting --banked
cip-composer eval-get <asset-slug>
```

The MCP tools are `record_evaluation`, `search_evaluations` and `get_evaluation`. In a PDR build, `search_for_requirement` recalls banked evaluations by problem type next to the registry candidates, and choosing BUILD over a banked match is flagged at lock time. Overwriting an evaluation's verdict, banked flag, problem types or evidence depth leaves an audit event with the before and after.

### Building an app from a PDR

Hand an LLM a PDR (product design requirements) document and it builds the app, with CIP as the system of record. The LLM designs and writes code. CIP records, judges, gates and traces. The spec is [`docs/pdr_to_build_spec_v1_1.md`](docs/pdr_to_build_spec_v1_1.md); the agent's instructions are the `cip-pdr-build` skill in [`plugins/cip-composer/skills/`](plugins/cip-composer/skills/).

The sequence:

1. **Agent:** `pdr_status` (first call of every session), `ingest_pdr`, `record_requirements` with verbatim quotes that CIP locates in the PDR itself, `record_capability_needs`, `request_requirements_confirmation`.
2. **You:** confirm the scope at the terminal, including what will be searched for. *Gate 1.*
3. **Agent:** `search_for_requirement` for each need (the mandatory sweep), `record_decision`, `pdr_approval_brief`, then `lock_architecture` to request the lock.
4. **You:** approve the lock. *Gate 2.*
5. **Agent:** `lock_architecture` again with the approved request id, which writes the lock and authorises BUILD needs in your name. Then `record_build_progress` for each task, and `check_against_lock` before touching anything locked.
6. **You or the agent:** `cip-composer verify` to have CIP run an acceptance test itself; `pdr-coverage` for where the build stands.

What each part guarantees:

| Piece | Ships |
|-------|-------|
| Requirements | PDR text and file hash stored; every requirement anchored to a verbatim quote CIP locates itself (one sentence, one section); PDR sections with no requirement, or barely quoted, reported |
| Capability needs | The unit that is searched and decided, linked N:M to requirements; its search terms are part of what you confirm, and are plain ASCII so what is confirmed is what is asked |
| Search | Logged every time, including a search that finds nothing; recalls banked evaluations by problem type; says when it hit its ceiling of 100 matches per term |
| Decisions | CIP computes through the Step 12 rules, and stands behind the result only where it holds the evidence (the same rows its matcher reads): for the candidate it chose, or for every candidate before it says nothing fits. An LLM override or judgment is labelled as one and reaches you with what CIP computed and the agent's reason |
| Human gates | Approved only from an interactive terminal, bound to the exact content and to the point in history it was approved for (the lock it replaces, the request it follows, the confirmation it was made under). A second confirmation and a relock each show what changed |
| Architecture lock | Versioned, immutable, hashed over decisions, providers, costs, cross-cutting choices, scope and the architecture document; usable only while the scope it was approved for is the confirmed scope; a relock shows a complete diff |
| Build gate | A task needs an active, usable lock and, for a BUILD need, Step 13 authorisation in the approver's name |
| Verification | Per acceptance criterion, with commit and command. A pass counts while the task it was recorded against is live; a fail stands; `verified` lapses when its evidence does |
| Coverage and status | Explicit denominators; `pdr_status` lets a fresh session resume, including across a lock approval |

Your commands, run in your own terminal:

```bash
cip-composer approvals list                 # what is waiting for you
cip-composer approvals show <id>            # exactly what you are being asked to approve
cip-composer approvals approve <id>         # asks you to type a code taken from the content
cip-composer approvals reject <id>          # asks what needs to change
cip-composer verify --task <task-id> --req <requirement-slug> --criterion 1 -- pytest tests/test_x.py::test_y
cip-composer pdr-status <project-name-or-id>
cip-composer pdr-coverage <project-id>
```

- **Approvals work only from an interactive terminal**, and no MCP tool grants one. Do not ask the agent to run them. On a machine where the agent has a shell, add a deny rule for `cip-composer approvals approve` and `cip-composer approvals reject` to its permission settings.
- `show` tells you which document the requirements came from (file and SHA-256, or that the agent supplied the text inline), whether that file has changed since, what changed since you last confirmed, and whether the request has been overtaken. For a lock it does the same for the architecture document. Only the most recent lock request can be approved or written.
- CIP wraps long lines itself, and every continuation row starts with `    | `. Anything behind that mark is the rest of the line above it, whatever it says. Read approvals in a terminal at least 60 columns wide, in the default view (not `--json`), and not through a viewer that re-flows text.
- `verify` runs from the app's repository on a committed tree. A run with uncommitted changes, before the command or left behind by it, is recorded but does not count as CIP-executed. `pdr-coverage` lists the command behind every CIP-executed pass and the repository it ran in. Read them: CIP does not know which repository is the project's.
- Document paths given to the server must be absolute, and only `.md`, `.markdown`, `.txt`, `.rst` and `.text` files up to 2 MB are read. Set `CIP_DOCUMENT_ROOTS`: without it the server reads any such file it can reach when an agent names one.
- The PDR commands exit 0 on success; 2 when an item was invalid, the request is blocked, or a lock check did not come back `allowed`; 1 for an error or a refusal.
- Most agent steps also have a CLI form (`ingest-pdr`, `record-requirements`, `record-needs`, `confirm-requirements`, `search-for-requirement`, `record-decision`, `pdr-brief`, `pdr-lock`, `check-against-lock`, `record-build-progress`) as a developer fallback.

### FAIR (optional free-model routing)

[FAIR](https://github.com/Rasatabula1971/FAIR-Free-AI-Router) is an embeddable router to free, quality-verified models. With it installed and at least one usable provider, CIP can draft requirements from a PDR, suggest a verdict for a need, and analyse a source file for capabilities.

```bash
pip install -e ".[fair]"        # Python 3.12 or later
cip-composer fair-status        # what FAIR is using, what it skipped, and why
```

The commands are `fair-extract`, `fair-suggest-verdict`, `fair-analyze` and `fair-status`; the MCP tools are `fair_extract_requirements`, `fair_suggest_verdict`, `fair_analyze_source` and `fair_status`. They write nothing. A draft still goes through `record_requirements`, and a suggestion through `record_decision` as the agent's proposal, under the same checks as anything else the agent sends. If FAIR is missing or has no provider, these tools say why, and nothing else in CIP is affected.

Providers:

- **Usable on their own:** `OPENROUTER_API_KEY` and `KILO_API_KEY` (free models only), and local Ollama with `OLLAMA_ENABLED=1` (`OLLAMA_URL` defaults to `http://127.0.0.1:11434`; the daemon must be running with a chat model pulled).
- **Recurring free-plan accounts:** `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `CLOUDFLARE_API_TOKEN`. A key alone is not enough. FAIR uses one only when its provider id (`google_gemini_api`, `groq`, `mistral`, `cloudflare_workers_ai`) is listed in `FAIR_CONFIRMED_FREE_PROVIDERS`, and listing it is your statement that the account behind the key cannot be billed. Do not list one just because the provider has a free tier.
- `FAIR_CLIENT_ID` tags requests (default `cip`).

## Adding CIP to another project

CIP ships in three shapes:

- **Python package:** `pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git` puts `cip-mcp`, `cip-composer` and `cip-bootstrap` on PATH.
- **MCP server:** `claude mcp add cip -- cip-mcp` after installing.
- **Claude plugin:** `claude plugin marketplace add <path-or-git-url>/plugins`, then `claude plugin install cip-composer`. It bundles the MCP server with both skills: `cip-composer`, which teaches Claude when to search CIP and how to hold the conversation, and `cip-pdr-build`, which drives the PDR workflow. Without the plugin, copy a skill's `SKILL.md` from [`plugins/cip-composer/skills/`](plugins/cip-composer/skills/) into `~/.claude/skills/<skill-name>/`.

All three need a Postgres with the CIP schema applied (`cip-bootstrap`). Full walkthrough: [`docs/INSTALL_IN_OTHER_PROJECTS.md`](docs/INSTALL_IN_OTHER_PROJECTS.md). Scheduled ingestion: [`docs/ingestion_cadence.md`](docs/ingestion_cadence.md).

## What's built

### Build workflow (Steps 0–14)

| Step | Status |
|------|--------|
| 0. Scaffold, env, import-linter contracts | ✅ |
| 1. Migration 001: provenance chain | ✅ |
| 2. Migration 002: workflow engine tables | ✅ |
| 3. Workflow engine (transitions, ledger, leases, outbox, retry, dead-letter) | ✅ |
| 4. Connector base (Protocol + registry), GitHub connector, FakeConnector | ✅ |
| 5. Ingestion state machine, identified → snapshotted | ✅ |
| 6. Static analysis and evidence extractors (append-only) | ✅ |
| 7. Model analysis, single provider (Gemini) | ✅ |
| 8. Capability registry (normalization, supersession) | ✅ |
| 9. Deterministic scoring (profile-as-data, byte-stable hash) | ✅ |
| 10. Policy gates, risk acceptance, publication guard | ✅ |
| 11. Lifecycle (candidate → cataloged, seven guards) | ✅ |
| 12. Projects, requirements, fit evaluation, recommendations | ✅ |
| 13. Search-before-build gate | ✅ |
| 14. LLM-in-chat via the MCP server. Replaces the planned FastAPI + React slice; `api/` holds stubs only | ✅ |

### Foundation phases (composable-component layer)

| Phase | Status | Ships |
|-------|--------|-------|
| 1. Component model | ✅ | Six kinds (library, repo, agent, skill, mcp_tool, workflow_template), runtime, cost_tier, license_spdx, JSONB I/O types |
| 2. Ingestion beyond libraries | ✅ | Idempotent GitHub, awesome-list, MCP-registry, Claude-skills and Gemini-enrich ingesters; run-cursor tracking; cadence doc |
| 3a. LLM classification | ✅ | component_kind and capability_kind reclassification with confidence and audit |
| 3b. Cross-kind identity | ✅ | `same_component` links across pypi, npm and github |
| 3c. Supersession detection | ✅ | Deprecation signals and successor resolution from descriptions |
| 3d. Per-kind scoring | ✅ | Deterministic health scores per component_kind |
| 4. Project constraints | ✅ | `project_constraint` (cpu_only, must_be_free_tier, license_allowlist, budget_monthly_ceiling, ...) as hard filters in browse and search |
| 5a. Workflow template steps and compatibility | ✅ | Chain schema; deterministic compatibility evaluator with adapter hints |
| 5b. `suggest_pipeline` | ✅ | Gemini decomposes an intent into stages with a pick per stage, edges and gap notes |
| 5c. `scaffold_pipeline` | ✅ | Turns a proposal into a repo skeleton |
| 6. Distribution and self-improvement | ✅ | `cip-composer` CLI, Claude skill, a demo pointing CIP at itself |

### Since the foundation phases

| Piece | Where |
|-------|-------|
| PDR-to-Build v1.0 (migration 028), evaluation store (029), first FAIR integration | PR #1 |
| FAIR rewired for the embedded `fair` module | PR #2 |
| PDR-to-Build v1.1 gap closure (migration 030) and four independent reviews | PR #3, [spec §14–19](docs/pdr_to_build_spec_v1_1.md#14-what-v10-got-wrong-and-where-it-was-closed) |
| FAIR passes the free-tier confirmation through | PR #4 |

## Layout

```
.
├── mcp_server/         MCP server (cip-mcp): registry, evaluation and PDR tools
├── cip_composer/       cip-composer CLI, including the human-only approvals commands
├── core/               Domain logic: no HTTP, no framework, no database in core/pdr
│   ├── workflow/       Workflow engine (Step 3)
│   ├── capability/     Registry, normalization, supersession (Step 8)
│   ├── scoring/        Deterministic scoring, profile-as-data (Step 9)
│   ├── policy/         Gates, risk acceptance, publication guard, constraints, compatibility
│   ├── judgment/       Model-analysis contracts (Step 7)
│   ├── project/        Requirements, fit, recommendations (Step 12)
│   └── pdr/            PDR-to-Build rules: spans, decisions, lock, lock check, tasks, coverage
├── workers/            Queue consumers and DB orchestrators (Step 5+)
├── connectors/         Source ecosystems: github, mcp, fake (Step 4+)
├── analysis/           Static analysis, evidence extractors, analyze-local (Step 6+)
├── integrations/       FAIR client
├── scripts/
│   ├── ingest/         Repeatable ingesters (Phase 2)
│   ├── judge/          LLM classifiers, linkers, deprecation detectors (Phase 3a–3c)
│   ├── score/          Per-kind scoring worker (Phase 3d)
│   ├── compose/        suggest_pipeline and scaffold_pipeline (Phase 5b/5c)
│   ├── bootstrap.py    cip-bootstrap
│   └── seed_*.py       Demo registry and bundled evaluations
├── db/                 Numbered migrations and connection helpers
├── config/             providers.yaml, scoring profiles, recommendation rules
├── plugins/            Claude plugin marketplace: cip-composer plugin and its two skills
├── api/                FastAPI stubs, superseded by mcp_server and cip_composer
├── tests/              pytest suite (needs Postgres)
├── docs/               See Documentation below
├── run-cip-mcp.*       Start the MCP server from the repo folder
└── setup-global-mcp.*  Install and register CIP at user scope in Claude Code
```

## Rules that outrank the layout

- `core/` must not import from `api/` or `workers/`. Enforced by import-linter.
- `core/scoring/` must have no import path to any provider client. Same enforcement.
- Schema changes are migrations, and an applied migration is never edited. 030's pre-release edits are the one exception, and it is closed ([Upgrading](#upgrading-a-database-that-already-has-cip-on-it)).
- `state_transition` and `evidence_item` are append-only, enforced in the database. So are the PDR workflow's search log, decision log, approvals and audit events. An `architecture_lock` row is immutable.
- No MCP tool records a human approval. Approvals come from `cip-composer approvals` at an interactive terminal.
- Every LLM-writable table records `prompt_name`, `prompt_version`, `prompt_hash` and `model_name` for audit. Bumping a prompt version re-runs cleanly.
- Every ingester tracks a run cursor in `ingest_run` and is safe to schedule.

## Documentation

| Document | What it is |
|----------|------------|
| [`docs/WHAT_IS_CIP.md`](docs/WHAT_IS_CIP.md) | Plain-language overview |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Technical reference, including the FAIR integration |
| [`docs/INSTALL_IN_OTHER_PROJECTS.md`](docs/INSTALL_IN_OTHER_PROJECTS.md) | Adding CIP to another build |
| [`docs/pdr_to_build_spec_v1_1.md`](docs/pdr_to_build_spec_v1_1.md) | PDR-to-Build spec as built, its limits (§12) and the four reviews (§16–19) |
| [`docs/ingestion_cadence.md`](docs/ingestion_cadence.md) | Ingesters and scheduled-task recipes |
| [`docs/v2_audit_report.md`](docs/v2_audit_report.md) | Phase 0 audit, 2026-09-10. A snapshot, not current state |
| [`mcp_server/README.md`](mcp_server/README.md) | MCP server notes. Its tool table is partial; `server.py` is the reference |
| [`plugins/cip-composer/README.md`](plugins/cip-composer/README.md) | The Claude plugin |

## License

MIT. See [`LICENSE`](LICENSE).
