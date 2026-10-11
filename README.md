# Capability Intelligence Platform

## How It Works (Plain English)

CIP is a **librarian for code**. When you're building software, you constantly need to answer questions like "is there already something that does this?", "what functions does this library give me?", "will this package work with my project's rules?", and "this thing broke — what can I replace it with?" CIP answers all of those by scanning codebases, cataloguing what it finds, and letting you search through it.

### Scanning

You point CIP at a directory of code. It reads every file — Python, TypeScript, JavaScript, whatever — and runs a set of "extractors" over them. Think of extractors as specialists: one finds package manifests (setup.py, package.json) to learn what the project is and what it depends on; one finds exported functions, classes, and interfaces — the public API of the code; one classifies each symbol by role (is this a React component? A middleware? A utility function? An API endpoint?); others check for licenses, test coverage, and secrets accidentally left in the code.

### Cataloguing

Everything the extractors find gets stored in a registry (a database). Each piece of code becomes a searchable "capability" with metadata: what language it's in, what it costs, what license it uses, what it depends on.

### Querying

When you need something, you search the registry. You can search by keyword, filter by language or role, or ask higher-level questions like "will this library conflict with that one?" or "does this fit my project's constraints?"

### Using It When Developing Other Apps

- **"What's available?"** — Before writing anything new, run `cip-composer analyze-local ./my-codebase`. It scans your existing code and tells you what's already there — utility functions, data models, API endpoints, etc. This prevents you from reinventing things you already have.
- **"I need an HTTP client"** — Search the registry: `cip-composer find "http client" --runtime python_import --cost-tier free`. CIP returns ranked results with license info, health scores, and whether each option fits your project's constraints (maybe you require only MIT-licensed code, or you can't use anything that needs a GPU).
- **"Will this work with what I have?"** — Before adopting a library, check compatibility: does it conflict with your existing dependencies? Does its license match your policy? Does it need system libraries you don't have? CIP answers all of this without you manually reading docs.
- **"Something broke"** — A package you depend on releases a breaking change. Tell CIP what failed, and it suggests replacements — ranked by how well they fit your project, how healthy they are, and whether other projects have had good experiences with them.
- **"Build me a pipeline"** — Describe what you want in plain English: "I need to fetch data from an API, transform it, and store it in a database." CIP decomposes that into stages and suggests a component for each one from the registry, checking that adjacent stages are compatible.

The `analyze-local` command is the simplest entry point: point it at any folder, and in seconds you get a map of everything CIP can see — every function, every interface, every symbol, organized by type and language. No database setup needed, no configuration. Just scan and learn.

---

The specs (Build Workflow, PDR, Data Model, State Machine) are the source of truth; this code is one path through them.

Two layers ship in this repo:

- **Steps 0–13** — the Build Workflow foundation: provenance, workflow engine, connectors, analysis, judgment, capability registry, scoring, gates, lifecycle, projects, search-before-build gate.
- **Foundation Phases 1–6** — the composable-component layer built on top: component kinds beyond libraries, repeatable ingestion, LLM-assisted classification/supersession/linking, per-kind scoring, project constraints, pipeline composition + scaffolding, and a Claude skill + CLI (`cip-composer`).

- **PDR-to-Build workflow** — hand an LLM a PDR and have it build the app with CIP as the system of record: requirements anchored to the PDR text, a reuse search before anything is built, two human approval gates, an immutable architecture lock, and coverage of what is implemented and verified. Spec: [`docs/pdr_to_build_spec_v1_1.md`](docs/pdr_to_build_spec_v1_1.md).

**New to this repo?** Read [`docs/WHAT_IS_CIP.md`](docs/WHAT_IS_CIP.md) for a plain-language overview, or [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the technical reference.

## What's built

### Build Workflow (Steps 0–13)

| Step | Status |
|------|--------|
| 0. Scaffold, env, import-linter contracts | ✅ |
| 1. Migration 001 — provenance chain | ✅ |
| 2. Migration 002 — workflow engine tables | ✅ |
| 3. Workflow engine (transitions, ledger, leases, outbox, retry, dead-letter) | ✅ |
| 4. Connector base (Protocol + registry) + GitHub connector + FakeConnector | ✅ |
| 5. Ingestion state machine — identified → snapshotted | ✅ |
| 6. Static analysis + evidence extractors (append-only) | ✅ |
| 7. Model analysis, single provider (Gemini) | ✅ |
| 8. Capability registry (normalization + supersession) | ✅ |
| 9. Deterministic scoring (profile-as-data, byte-stable hash) | ✅ |
| 10. Policy gates + risk acceptance + publication guard | ✅ |
| 11. Lifecycle (candidate → cataloged, seven guards) | ✅ |
| 12. Projects, requirements, fit evaluation, recommendations | ✅ |
| 13. Search-before-build gate | ✅ |
| 14. LLM-in-chat via MCP server | ✅ (extended by Phases 1–6 below) |

### Foundation Phases (composable-component layer)

| Phase | Status | Ships |
|-------|--------|-------|
| 1. Component model | ✅ | Six kinds (library/repo/agent/skill/mcp_tool/workflow_template), runtime, cost_tier, license_spdx, JSONB I/O types |
| 2. Ingestion beyond libraries | ✅ | Idempotent GitHub / awesome-list / MCP-registry / Claude-skills / Gemini-enrich ingesters, run cursor tracking, cadence doc |
| 3a. LLM classification | ✅ | Component_kind + capability_kind reclassification with confidence + audit |
| 3b. Cross-kind identity | ✅ | `same_component` links between pypi/npm/github ecosystems |
| 3c. Supersession detection | ✅ | Deprecation signals + successor resolution from descriptions |
| 3d. Per-kind scoring | ✅ | Deterministic health scores per component_kind (repo/library/skill/mcp_tool/agent/workflow_template) |
| 4. Project constraints | ✅ | `project_constraint` (cpu_only, must_be_free_tier, license_allowlist, budget_monthly_ceiling, …) with hard-filter integration in browse/search |
| 5a. workflow_template_step + compatibility | ✅ | Chain schema; deterministic compat evaluator with adapter hints |
| 5b. suggest_pipeline | ✅ | Gemini decomposes intent → per-stage picks with edges + gap notes |
| 5c. scaffold_pipeline | ✅ | Turns a proposal into a real repo skeleton |
| 6. Distribution + self-improvement | ✅ | `cip-composer` CLI + Claude skill + meta demo pointing CIP at itself |

### PDR-to-Build workflow (spec v1.1)

| Piece | Ships |
|-------|-------|
| Requirements | PDR text and file hash stored; every requirement anchored to a verbatim quote CIP locates itself (one sentence, one section); PDR sections with no requirement, or barely quoted, reported |
| Capability needs | The unit that is searched and decided, linked N:M to requirements; its search terms are part of what the human confirms, and are plain ASCII so that what is confirmed is what is asked |
| Search | Logged every time, including a search that finds nothing; recalls banked evaluations by problem type; says when it hit its ceiling of 100 matches per term |
| Decisions | CIP computes through the Step 12 rules, and stands behind the result only where it holds the evidence (the same rows its matcher reads): for the candidate it chose, or for every candidate before it says nothing fits; an LLM override or judgment is labelled as one and reaches the human with what CIP computed and the agent's reason |
| Human gates | Requirements confirmation and architecture lock, approved only from a terminal (`cip-composer approvals`), bound to the exact content approved and to the point in history it was approved for (the lock it replaces, the request it follows, the confirmation it was made under); a second confirmation and a relock each show what changed; CIP lays the summaries out itself, so agent-written text stays inside its own field |
| Architecture lock | Versioned, immutable, hashed over decisions, providers, costs, cross-cutting choices, scope and the architecture document; usable only while the scope it was approved for is the confirmed scope; a relock shows a complete diff |
| Build gate | A task needs an active lock and, for a BUILD need, Step 13 authorisation in the approver's name |
| Verification | Per acceptance criterion, with commit and command; a pass counts while the task it was recorded against is live, a fail stands, and `verified` lapses when its evidence does; `cip-composer verify` runs the command itself and records what it saw |
| Coverage + status | Explicit denominators; `pdr_status` lets a fresh session resume, including across a lock approval |

The coding agent's instructions are the `cip-pdr-build` skill in [`plugins/cip-composer/skills/`](plugins/cip-composer/skills/).

## Quickstart (local, with a running Postgres)

You need a running Postgres 14+ and two empty databases: `cip_local` and `cip_test`.
The defaults in `.env.example` assume a local user `cip` with password `cip` on port `5432`.

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env
# edit .env — set DATABASE_URL and TEST_DATABASE_URL to match your Postgres install
pip install -e .            # installs the `cip-mcp` and `cip-composer` scripts
$env:PYTHONPATH = "."
python -m db.migrate up     # applies every migration in db/migrations in order
pytest                      # runs the whole suite
lint-imports                # enforces the two structural rules
```

**macOS / Linux (bash):**

```bash
cp .env.example .env
pip install -e .
export PYTHONPATH=.
python -m db.migrate up
pytest
lint-imports
```

Notes:
- Windows Postgres installer sometimes claims port `5433` if `5432` is taken. Check with `netstat -ano | findstr LISTEN` and update the port in `.env`.
- `GITHUB_TOKEN` is optional — public-repo reads work without it, but a token raises the rate limit substantially. Set via user env vars, not in `.env`, if you want to avoid leak risk during a Claude session.
- `GEMINI_API_KEY` is needed for Step 7 (judgment), Phase 2e (enrichment), Phase 3a/3b/3c (classification/linking/supersession), and Phase 5b (pipeline suggestion). Same secret-hygiene rule.

## Using it

Once installed and the migrations applied:

```powershell
cip-composer info                                     # registry stats
cip-composer find "video editing" --kind repo --limit 10
cip-composer suggest "transcribe audio then subtitle it" --out proposal.json
cip-composer scaffold --proposal proposal.json --out-dir ./my-pipeline
cip-composer flow "intent here" --out-dir ./my-pipeline    # all in one
```

From inside a Claude Code session with the CIP MCP server registered (`claude mcp add cip -- cip-mcp`), the same surface is available as MCP tools:
`mcp__cip__search_capabilities`, `browse_components`, `capability_detail`, `capability_constraint_fit`, `capability_compatibility`, `suggest_pipeline`, `scaffold_pipeline`.

A companion Claude skill lives at `~/.claude/skills/cip-composer/SKILL.md` — it teaches Claude when to invoke CIP and how to hold the conversation.

### Building from a PDR

The agent drives the workflow through the `cip` MCP tools, starting with `pdr_status`. You have two jobs, both in your own terminal:

```powershell
cip-composer approvals list                 # what is waiting for you
cip-composer approvals show <id>            # exactly what you are being asked to approve
cip-composer approvals approve <id>         # asks you to type a code from the content
cip-composer approvals reject <id>          # asks what needs to change
```

Approvals only work from an interactive terminal, and there is no MCP tool that grants one. Do not ask the agent to run these. `show` tells you which document the requirements came from (file and SHA-256, or that the text was supplied inline), whether that file has changed since, what changed since you last confirmed, and whether the request has been overtaken by a later one. For a lock it does the same for the architecture document. Only the most recent lock request can be approved or written.

Long lines in a summary are wrapped by CIP, and every continuation row starts with `    | `. Anything behind that mark is the rest of the line above it, whatever it says. Every line the PDR commands print is wrapped this way, not only the summary. Read approvals in a terminal at least 60 columns wide, in the default view (not `--json`), and not through a viewer that re-flows text.

To have CIP run an acceptance test itself, from the app's repository, on a committed tree:

```powershell
cip-composer verify --task <task-id> --req <requirement-slug> --criterion 1 -- pytest tests/test_x.py::test_y
cip-composer pdr-status <project-name>      # where the build is
cip-composer pdr-coverage <project-id>      # how much of the PDR is built, on what evidence
```

`verify` proves one thing: that command exited 0 at that commit. A run with uncommitted changes, before the command or left behind by it, is recorded but does not count as CIP-executed. `pdr-coverage` lists the command behind every CIP-executed pass and the repository it ran in; read them. CIP does not know which repository is the project's.

The PDR commands exit 0 on success; 2 when an item was invalid, the request is blocked, or a lock check did not come back `allowed`; 1 for an error or a refusal.

Recommended: set `CIP_DOCUMENT_ROOTS` (a path list) to confine the folders the server may read PDR and architecture documents from. Without it the server reads any `.md`, `.txt` or `.rst` file it can reach when an agent names one.

## Adding CIP to another project

CIP ships in three shapes so you can add it to any build:

- **Python package** — `pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git` (drops `cip-mcp`, `cip-composer`, and `cip-bootstrap` on PATH)
- **MCP server** — `claude mcp add cip -- cip-mcp` after installing
- **Claude plugin** — bundles the skill and the MCP server together via `claude plugin install cip-composer` (marketplace lives at [`plugins/`](plugins/))

Full walkthrough: [`docs/INSTALL_IN_OTHER_PROJECTS.md`](docs/INSTALL_IN_OTHER_PROJECTS.md).
Prerequisite for all three: a Postgres 14+ instance with the CIP schema applied.

## Ingestion cadence

See [`docs/ingestion_cadence.md`](docs/ingestion_cadence.md) for scheduled-task recipes.

## Layout

```
.
├── api/                FastAPI app — stubs only (superseded by mcp_server + cip_composer)
├── mcp_server/         MCP server exposing the registry to LLMs
├── cip_composer/       cip-composer CLI package
├── core/               Domain logic — no HTTP, no framework imports
│   ├── workflow/       Workflow engine (Step 3)
│   ├── capability/     Registry, normalization, supersession (Step 8)
│   ├── scoring/        Deterministic scoring, profile-as-data (Step 9)
│   ├── policy/         Gates, risk acceptance, publication guard, constraints, compatibility (Steps 10 + Phases 4/5a)
│   ├── judgment/       Model-analysis contracts (Step 7)
│   ├── project/        Requirements, fit, recommendations (Step 12)
│   └── pdr/            PDR-to-Build rules: spans, decisions, lock, lock check, tasks, coverage
├── connectors/         Source ecosystems — github, mcp, fake (Step 4+)
├── analysis/           Static analysis + evidence extractors (Step 6+)
├── workers/            Queue consumers (Step 5+)
├── scripts/
│   ├── ingest/         Repeatable ingesters (Phase 2)
│   ├── judge/          LLM classifiers, linkers, deprecation detectors (Phase 3a/3b/3c)
│   ├── score/          Deterministic per-kind scoring worker (Phase 3d)
│   └── compose/        suggest_pipeline + scaffold_pipeline (Phase 5b/5c)
├── db/                 Numbered migrations and connection helpers
├── config/             providers.yaml, scoring_profiles/, recommendation_rules/
├── tests/              pytest suite
└── docs/               ingestion_cadence.md and future architecture notes
```

## Rules that outrank the layout

- `core/` must not import from `api/` or `workers/`. Enforced by import-linter.
- `core/scoring/` must have no import path to any provider client. Same enforcement.
- Schema changes are migrations. Never alter a table in place.
- `state_transition` and `evidence_item` are append-only (enforced at the DB level). So are the PDR workflow's search log, decision log, approvals and audit events; an `architecture_lock` row is immutable.
- No MCP tool records a human approval. Approvals come from `cip-composer approvals` at an interactive terminal.
- Every LLM-writable table records `prompt_name + prompt_version + prompt_hash + model_name` for audit. Bumping a prompt version re-runs cleanly.
- Every ingester tracks a run cursor in `ingest_run` and is safe to schedule.
