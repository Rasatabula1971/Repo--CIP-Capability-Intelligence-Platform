# CIP MCP Server

Exposes the CIP capability registry as MCP tools an LLM can call. The point
of Step 14 (path B): the LLM lives in the user's chat client (Claude,
ChatGPT), the registry lives here, and this server is the bridge.

## Tools

| Tool | What it returns |
|------|-----------------|
| `search_capabilities(query, ecosystem?, kind?, limit=20)` | Ranked list — display name, normalized key, ecosystem, kind, head version, and intrinsic score/confidence when a scorecard exists. |
| `capability_detail(capability_id)` | Full record for one capability: metadata, head version, interfaces, declared dependencies, and the head version's scorecard. Every interface and dependency carries an `evidence_item_id`. |

The server now exposes many more tools than this table; `mcp_server/server.py` is the reference, and each tool's docstring is its contract.

### PDR-to-Build workflow

Used in this order. Full contracts in [`docs/pdr_to_build_spec_v1_1.md`](../docs/pdr_to_build_spec_v1_1.md).

| Tool | Purpose |
|------|---------|
| `pdr_status` | Where a project is and what is allowed next. First call of every session. |
| `ingest_pdr` | Store the PDR text as the provenance root. Takes an absolute path to a text document. |
| `record_requirements` | PDR requirements, each anchored to a verbatim quote of at most 600 characters from one section. |
| `record_capability_needs` | The reusable capabilities the build needs, with their search terms (ASCII letters, digits, single spaces and `+ # . - /`), linked to requirements. A license allowlist takes SPDX identifiers. |
| `retire_requirement` | Take a requirement or need out of scope. |
| `request_requirements_confirmation` | Human gate 1. |
| `search_for_requirement` | Registry and evaluation-store search for one need. Always logged. Up to 100 matches per term; `truncated` says when there were more. |
| `record_decision` | CIP computes the verdict where it holds the evidence; the LLM may propose another. Refused if the registry or the evaluation store changed since the sweep. |
| `pdr_approval_brief` | Preview what the lock would freeze. Read-only. |
| `lock_architecture` | Human gate 2. Called twice, with the approval in between; the second call passes only `approval_request_id`. Only the latest request can be approved or written. Writing a lock abandons the tasks it does not carry (`tasks_abandoned`). |
| `record_build_progress` | Create a task (the build gate); report status and verification. |
| `check_against_lock` | Test a proposed change against the lock. Logged. |
| `pdr_coverage` | How much of the PDR is built, and on what evidence. |

There is no tool that records a human approval, and none takes an approver or an approval note. That is done in a terminal with `cip-composer approvals`.

The four `fair_*` tools call a free model and write nothing. `fair_extract_requirements` returns drafts in the shape `record_requirements` takes; `fair_suggest_verdict` returns a suggestion to pass to `record_decision` as a proposal. Both are checked there like anything else the agent proposes.

Older tools on this server that write into what the workflow reads are held to it: `import_requirements` is refused for a project with a PDR, `authorize_build` refuses a capability need, files written by `scaffold_pipeline` are logged (also when it fails halfway) and labelled if one is ingested as a PDR, and `record_evaluation` leaves an audit event when it changes an evaluation.

Document paths (`pdr_path`, `architecture_doc_path`) must be absolute: the server does not run in the project's folder. Only `.md`, `.markdown`, `.txt`, `.rst` and `.text` files are read. `CIP_DOCUMENT_ROOTS` confines reading to listed folders; set it. If it is set and names no folder, nothing is read.

Ids are UUIDs in the usual lower-case hyphenated form. A call that loses a race with another session returns `{"error": ..., "retry": true}` and keeps nothing.

## Prerequisites

- Postgres running with the CIP migrations applied (see the top-level [README](../README.md#quickstart-local-with-a-running-postgres)).
- `.env` populated so `DATABASE_URL` resolves.
- The `cip` package installed in the environment you'll run the server from:

  ```powershell
  pip install -e .
  ```

## Register with Claude Code

```powershell
claude mcp add cip -- cip-mcp
```

If the `cip-mcp` script is not on PATH, use the module form:

```powershell
claude mcp add cip -- python -m mcp_server.server
```

After registering, `/mcp` inside Claude Code should show `cip` connected
with the tools listed above.

## Register with Claude Desktop

Add an entry to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cip": {
      "command": "cip-mcp",
      "cwd": "C:\\CIP\\Repo--CIP-Capability-Intelligence-Platform"
    }
  }
}
```

`cwd` matters — the server reads `.env` from the working directory.

## Smoke test without Claude

The tool functions are testable directly against a running Postgres:

```powershell
python -c "from db.connection import connect; from mcp_server.queries import search_capabilities; c = connect(); print(search_capabilities(c, 'http'))"
```
