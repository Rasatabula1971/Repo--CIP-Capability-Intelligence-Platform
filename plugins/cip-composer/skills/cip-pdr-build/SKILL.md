---
name: cip-pdr-build
description: Build an application from a PDR (product design requirements) with CIP as the system of record. Use when the user hands over a PDR, spec or requirements document and wants it built, when resuming a build CIP is already tracking, or when a change is needed to something already locked. CIP records requirements, searches for reusable capabilities before anything is built, freezes approved decisions in an architecture lock, and tracks what is implemented and verified against the PDR.
license: MIT
---

# Building from a PDR with CIP

You read the PDR, design the architecture and write the code. CIP records, judges, gates and traces. It never generates architecture or application code.

## Three rules that do not bend

1. **You never approve anything.** Two points need a human: confirming the requirements, and approving the architecture lock. A human does that in their own terminal with `cip-composer approvals`. Never run `cip-composer approvals approve` or `reject`, never call the approval code from Python, and never ask for a way around it. When a tool returns `awaiting_approval`, show the human the `summary_markdown`, tell them the command, and stop. You do not write the approval note either; no tool takes one.
2. **No code before the lock.** Do not write application code for a requirement until `mcp__cip__record_build_progress` has accepted a task for it.
3. **A refusal is an instruction.** Every `refused` result carries a `reason` that says what to do next. Do it. Do not retry the same call with different wording, and do not work around a gate by building anyway. Refusals are logged.

## Start every session here

Call `mcp__cip__pdr_status(project_name=...)` first. A build spans many sessions and you start each with no ids. The result gives you the phase, every id you need, pending human approvals, an approved lock request waiting to be written, the active lock and what it froze, and the `next_actions` the workflow will accept. If the project is not found, begin at step 1.

## The workflow

**1. Ingest the PDR.** `mcp__cip__ingest_pdr(project_name, pdr_title, pdr_path)`. Pass the **absolute** path of the file, not the text: the CIP server does not run in your project folder, and a relative path is refused. CIP stores the text so it can check your quotes, and records the file's hash so the human can see which document they are confirming. Text passed as `pdr_text` is shown to the human as "supplied inline by the agent"; use it only when there is no file. The file must be `.md`, `.markdown`, `.txt`, `.rst` or `.text`. The project name (60 characters at most) and the title (120) are each one plain line. The PDR is the human's document: do not write it yourself with another tool and ingest the result. CIP labels a file one of its own tools wrote.

**2. Record requirements.** `mcp__cip__record_requirements`. One atomic, testable requirement per item, each with:
- `slug`: always give one, 80 characters at most. It is how you update the requirement later. Without it CIP derives a slug from the text and refuses the item if that slug already exists.
- `quote`: the sentence in the PDR that states this requirement, copied verbatim. At most 600 characters, and from one section: a quote that runs across a heading is refused. CIP finds the quote and computes the span; a quote that is not in the PDR is refused. Do not send character offsets.
- `acceptance_criteria`: a list. Each criterion must be something a test can show. Required for anything that is in the build, constraints included.
- `kind`: `functional`, `non_functional` or `constraint`.

Write `text`, each criterion and each reason on one line; CIP stores a line break as a space. Changing a requirement's text, kind or quote later deletes the passes recorded against its criteria. Fails stay: rewording a requirement does not clear a failure.

Read the `span_coverage` that comes back. The human is shown the same three lists:
- `unmapped_sections`: parts of the PDR with no requirement mapped.
- `out_of_scope_only_sections`: parts quoted only by deferred or not-applicable items. Nothing in the build comes from them.
- `lightly_quoted_sections`: long sections where your quotes cover little of the text. A table of requirements mapped by one row shows up here.

Go back and check each one for requirements you missed.

**3. Sketch the architecture, then record capability needs.** Design first: you cannot know which reusable capabilities the build needs until you have a shape for it. Then `mcp__cip__record_capability_needs`. A need is a reusable capability such as auth, a job queue or PDF rendering. It is the thing CIP searches, because a sentence like "the receptionist sees today's bookings" has nothing to match on.
- `slug`: always give one.
- `search_terms`: 1 to 8 short keyword phrases, one phrase per term. Plain ASCII letters, digits, single spaces and `+ # . - /` only: no commas, quotes, `%` or `_`, and no accented letters or letters from another alphabet (such a term is refused, with the character named). Terms are stored in lower case. Choose them to find things: the human confirms them exactly as written, and they are what the registry is asked. Registry names and keys are ASCII, so write the technical name (`oauth`, `pdf render`), whatever language the PDR is in.
- `problem_types`: tags that recall banked evaluations (`oauth-flow`, `retry-with-backoff`). A need with none cannot be matched to a banked evaluation, and the human is told so.
- `satisfies`: the requirement slugs it serves.
- `constraints`: give them wherever you can (`license_allowlist`, `required_interface`, `forbidden_dependency`). Without a constraint CIP cannot compute a verdict and the decision is recorded as your judgment, not CIP's. A `license_allowlist` takes SPDX identifiers (`MIT`, `Apache-2.0`, `BSD-3-Clause`), not license names: `MIT License` is refused, and a misspelt identifier matches no license at all.

Every requirement must end up one of: linked to a need; `bespoke` (project-specific logic with nothing reusable to search for); `deferred`; or `not_applicable`. The last three need a reason and go through `mcp__cip__record_requirements`. Do not mark something `bespoke` to skip a search. The human sees every bespoke item and confirms it.

To drop a requirement or a need, use `mcp__cip__retire_requirement`. The human is shown what was retired and why.

**4. Human gate 1: confirm the scope.** `mcp__cip__request_requirements_confirmation`. If `status` is `blocked`, fix the `blockers`. If `awaiting_approval`, show the summary and stop. On a second confirmation the summary starts with what changed since the first; `changes_since_last_confirmation` gives you the same list.

Any later edit to a requirement, or to a need's search terms, problem types, constraints or links, re-opens this gate. So does going back to content that was confirmed earlier: an old approval does not carry over. While this gate is open the lock is stale too: no new tasks, no progress, no verification until the scope is confirmed again.

**5. Search each need.** `mcp__cip__search_for_requirement(req_id=need_id)` with nothing else. That is the mandatory sweep of the need's own confirmed search terms. A `search_query` or a filter is extra evidence and never replaces it. Read:
- `candidates`: evaluated registry candidates with fit and intrinsic scores. Each is marked `evidence: sufficient` or `insufficient`, with `missing_evidence` naming what CIP has not read for the revision it would pin (license, interfaces, dependencies, score). A LICENSE file CIP could not identify, or could only guess at, counts as not read. For an `insufficient` candidate the fit score means "CIP has not read this", not "this has a problem". `confirmed_blocking_gaps` are the gaps that rest on something CIP did read.
- `unevaluable`: found, but cannot be chosen: no source to pin, or excluded by the project's policy.
- `evaluation_matches`: banked evaluations from earlier work. Check `evidence_depth` before trusting one: `docs_only` means nobody read the code.
- `truncated`: a term returned 100 matches, the most the sweep takes. There are matches CIP did not look at. Narrowing the need's search terms is the fix, and they need confirming again.
- `coverage_outcome`: `no_registry_coverage` means CIP had nothing; `found_but_unevaluable` means it found matches it could not evaluate. Neither is proof that nothing exists. Say so to the human.

**6. Record each decision.** `mcp__cip__record_decision(req_id)`. CIP computes the verdict first. `verdict` is your proposal:
- Omit it, or agree, and the decision is CIP's (`cip_rules`).
- Disagree and it is an override (`llm_override`). Give a real `rationale`, in words a person can read.
- If CIP reports it cannot compute a verdict, propose one with a rationale (`llm_judgment`). That happens when the need has no checkable constraint, when CIP lacks the evidence for a candidate (for BUILD, any candidate: "nothing fits" is a claim about all of them), when the sweep was truncated, or when its rules reject the best-ranked candidate and would accept another. In that last case look at the candidate it names before you propose BUILD: a BUILD over a candidate the rules accept is flagged for the human. Your rationale is shown to the human word for word, next to what CIP computed. When you choose a candidate CIP has not analysed, check its license and interfaces yourself and say in the rationale what you checked.

Valid verdicts are ADOPT, ADAPT, WRAP, REFERENCE and BUILD. You can only choose a candidate a search returned. REJECT is not valid for a need: if nothing fits, propose BUILD. If anything the sweep read has changed since (a capability added, ingested, analysed or superseded, or an evaluation recorded), the call is refused: run the sweep again, then decide.

**7. Human gate 2: lock the architecture.** Settle the inputs first, and preview with `mcp__cip__pdr_approval_brief`. Then call `mcp__cip__lock_architecture` with the same inputs:
- `architecture_doc_path`: the absolute path of your architecture document. The human is shown the path and the file's hash.
- `approved_providers`: every external service, with its real category, cost and whether data leaves the user's infrastructure. Write the name the provider is known by in plain letters (`Okta`, `Stripe`). A name with letters outside ASCII is put in front of the human as one CIP could not check against its list of known providers.
- `cost_commitments` (one line per item), `project_decisions` (database, hosting, auth model), `unresolved_risks`.

The first call returns `awaiting_approval` and an `approval_request_id`. Show the summary and stop. Only the most recent lock request can be approved or written: asking again with different inputs replaces the one the human was reading, and an earlier request stays dead even if it had been approved. Asking for earlier content again opens a new request that needs its own approval.

After the human approves, call `mcp__cip__lock_architecture(project_id, approval_request_id=...)` with nothing else: it writes exactly what was approved. In a new session, `mcp__cip__pdr_status` gives you that id as `approved_lock_request`. If anything changed after the request was made (a decision, the requirements, or the requirements being confirmed again), the call is refused and you need a fresh request and a fresh approval.

Writing a lock abandons every task it does not carry: one whose requirement left the build, or whose decision changed. The result lists them as `tasks_abandoned`. Create new tasks for that work; an abandoned task does not come back under a later lock.

**8. Build.** For each piece of work, `mcp__cip__record_build_progress` in create mode:
- `req_ids`: the requirement(s) the task delivers, plus the capability need when the task integrates or builds one.
- `permitted_paths`: required. The repo-relative files and directories the task may touch, as narrow as the work. Each must start with a real directory or file name: `src/auth/` or `src/**`, never `**`.
- `reused_capability_version_id`: leave it out. CIP fills in the version the lock pinned and returns it. Use that version and no other.

The `title` is one plain line. Then report status as you go: `planned` to `in_progress` to `implemented` to `verified`. Move a task to `in_progress` before you report a pass against it; a pass on a planned task is refused, and a pass stops counting while its task is moved back to `planned`. An abandoned task is closed, and the passes recorded against it stop counting. A fail does not go away when its task is abandoned: only a later pass from live work clears it.

**9. Check before you change.** Call `mcp__cip__check_against_lock` before you add or swap a dependency, use a new external provider, change a locked capability, change a locked project decision, or edit a file outside the task's `permitted_paths`. Send structured input: `changed_paths` with `build_task_id`, `capability_changes`, `provider_changes`, `dependency_adds`, `project_decision_changes`. A description alone is refused, and anything the check does not recognise is escalated, not ignored.
- `allowed`: go ahead.
- `needs_approval`: stop. Re-decide what it touches with `mcp__cip__record_decision`, then `mcp__cip__lock_architecture`. The relock shows the human a diff from the active lock. That diff is the change request.
- `blocked`: fix what the reason names.

A REFERENCE decision lets you read a capability and learn from it. Installing it is a different decision.

Once you re-decide a need, work that rests on it is on hold until the relock is approved: tasks on that need take no progress and no verification, and the lock check answers `needs_approval` for its capability and its dependency. Work on other needs carries on.

**10. Verify.** A requirement is verified when its decision is the locked one, a live task has reported it implemented, and every acceptance criterion's latest verification is a pass. Report through `mcp__cip__record_build_progress` with `verification`: the criterion, `outcome`, `commit_sha` (the real commit hash), `test_ref`, `command` and `exit_code`. A pass without all of those is refused. Report fails too: the latest report wins, and `verified` does not outlive its evidence. `not_run` after a fail leaves the fail standing; only a pass clears it. A later fail, a new or reworded criterion, a rewritten requirement or an abandoned task all undo it. Nothing is recorded while the lock is stale.

What you report is recorded as `agent_reported`. Stronger evidence is `cip_executed`: the human, or you if they ask, runs the test through CIP in the target repo:

```
cip-composer verify --task <build_task_id> --req <slug> --criterion <n> -- pytest tests/test_x.py::test_y
```

CIP runs the command, and records the exit code, an output digest, the commit, the repository and whether the tree was dirty before or after the run. Run it in the project's own repository: coverage shows the human where each pass ran. Commit first: a run on a dirty tree is recorded but does not count as CIP-executed. The command is stored and shown in coverage. Run the test that exercises the criterion; never wrap a command that cannot fail.

**11. Report.** `mcp__cip__pdr_coverage`. Report its denominators with its numbers: unmapped, out-of-scope-only and lightly quoted PDR sections, what is deferred, how many needs the registry could answer, how many decisions were LLM judgment, how many verifications are agent-reported only. A REFERENCE is not reuse; report it separately.

## When the PDR changes

Ingest the new text with `mcp__cip__ingest_pdr` on the same project. Requirements whose quotes still appear are carried forward; the rest come back as `orphaned` and must be re-recorded or retired. The requirements need confirming again and the lock is stale until relocked. Tasks whose decisions did not change, and whose requirements are still in the build, carry over. The others are abandoned when the new lock is written. Going back to an earlier PDR works the same way: ingest that text again.

## What this does and does not enforce

Say this plainly if the human asks how strong the guardrails are.

CIP's gates bind because you call its tools. Nothing physically stops code being written outside them. What CIP does provide:
- Approvals are bound to the exact content approved and to the moment they were made for. Edit after approval, or let another lock be written first, and the approval no longer applies.
- No MCP tool can record an approval, and the CLI refuses a non-interactive caller.
- The human is shown everything that is locked. CIP lays the summary out itself, so text you write stays inside its own field.
- Every gate refusal, lock check, decision and verification is logged.
- `mcp__cip__pdr_coverage` reports tasks that were worked on with no lock check on record, and the command behind every verification.

What it does not provide: detection of code that drifted from the lock. That check, comparing the repository against the lock, is not built. `cip-composer verify` shows that a command exited 0 at a commit; it does not show the command tests the criterion. CIP computes a verdict only where it has read the evidence, and its search stops at 100 matches per term. Four independent reviews each found places where CIP told the human more than it had checked. Those are fixed, the fixes for the last round have not themselves been reviewed, and there will be others. Do not describe the lock, a verification or a search as more than it is.
