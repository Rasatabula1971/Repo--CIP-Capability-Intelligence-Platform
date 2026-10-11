# CIP vNext — PDR-to-Build Implementation Spec v1.1

**Purpose:** the workflow *"give an LLM a PDR, it uses CIP to help build the app."*
**Status:** as built. Supersedes v1.0. Migration `030_pdr_gap_closure.sql`, package `core/pdr/`, tools in `mcp_server/pdr_queries.py`.
**Audience:** whoever extends this, and the coding agent that drives it (the agent's own instructions are in `plugins/cip-composer/skills/cip-pdr-build/SKILL.md`).

v1.0 was implemented as migration 028 and nine MCP tools. A review of that slice found it could run green end to end while proving nothing. This version closes those holes; section 14 lists each one and where it was closed.

The v1.1 code was then handed, four times, to an independent reviewer whose only job was to get round it. The first found twenty ways (section 16). The second, working against the fixed code, found one critical hole beside the build gate, eight high and ten medium (section 17). The third found no way to obtain a lock or an approval the human had not given, and nine ways to make CIP or the human assert something false (section 18). The fourth, a narrower pass over what the third fix round had changed, again opened no gate, and found four more false assertions, eight bounded faults and eight smaller ones (section 19). All four lists are closed. **The fixes for the fourth have not themselves been reviewed.** Where this document says a thing is refused, logged or shown, there is a test that fails if it is not, and a bug-injection mutant that fails that test. Section 12 says what is still not enforced, and section 19 ends with what four rounds of review do and do not show.

---

## 0. Scope and guardrails

Unchanged from v1.0, with one clarification.

1. **CIP does not generate architecture or application code.** The driving LLM designs and codes. CIP records, judges, gates and traces. Generation stays outside CIP's audit boundary so the deterministic core stays deterministic (OD-16).
2. **No tables for derived outputs.** The approval brief, coverage and status are computed on every call. *Clarification:* a record that something happened is evidence, not a derived output. The search log, the decision log, approval requests and audit events are stored because they are what the derived views are derived from.
3. **Import-linter contracts hold.** `core/pdr/` is pure: no DB, no IO, no model imports. `core` still imports nothing from `api` or `workers`.
4. **The lock is governed cooperation, not a sandbox.** Section 12 says exactly what that means.

The verdict set is the six the `recommendation` table implements (ADOPT, ADAPT, WRAP, REFERENCE, REJECT, BUILD). At the level of a capability need only five are outcomes; see section 3.

---

## 1. Who does what

| Step | Owner | Mechanism |
|---|---|---|
| Read the PDR | LLM | its own context |
| Store the PDR text and the file's SHA-256 | **CIP** | `ingest_pdr`, from an absolute file path |
| Extract requirements with verbatim quotes | LLM | `record_requirements` |
| Locate each quote, compute its span, refuse what is not in the PDR | **CIP** | `core.pdr.spans` |
| Sketch the architecture | LLM | free-form, outside CIP |
| Name the capability needs, their search terms, and link them to requirements | LLM | `record_capability_needs` |
| Confirm the scope, including what will be searched for | **Human** | `cip-composer approvals` |
| Search registry and evaluation store, log the search | **CIP** | `search_for_requirement` |
| Compute the verdict, where there is evidence to compute it from | **CIP** | Step 12 rules via `workers.recommend` |
| Propose a different verdict, with a rationale | LLM | `record_decision` |
| Approve the lock, including every override | **Human** | `cip-composer approvals` |
| Freeze decisions; authorise BUILD needs (Step 13) | **CIP** | `lock_architecture` |
| Write code and tests | LLM | in the target repo |
| Gate each task; test changes against the lock | **CIP** | `record_build_progress`, `check_against_lock` |
| Run an acceptance test and observe the result | **CIP** | `cip-composer verify` |
| Answer "how much of the PDR is built?" | **CIP** | `coverage` |

---

## 2. The model: requirements and capability needs

v1.0 searched the registry with requirement sentences. Step 12's fit matchers work on required interfaces, license allowlists and forbidden dependencies. "The receptionist can see today's bookings" has none of those, so the search returned noise or nothing.

Two kinds of row now share `project_requirement`:

**PDR requirement** (`kind` = `functional`, `non_functional` or `constraint`). What the PDR asks for. Carries a source quote, a priority, acceptance criteria and a **disposition**:

| Disposition | Meaning | Set by |
|---|---|---|
| `via_needs` | satisfied through linked capability needs | linking a need |
| `bespoke` | project-specific logic; nothing reusable to search for | `record_requirements`, with a reason |
| `not_applicable` | not a build item | `record_requirements`, with a reason |
| `deferred` | out of this build, on the record | `record_requirements`, with a reason |

A requirement that is in the build (`via_needs` or `bespoke`) needs at least one acceptance criterion, whatever its kind. Only `deferred` and `not_applicable` may go without.

**Capability need** (`kind` = `capability_need`). A reusable capability the build needs: auth, a job queue, PDF rendering. It carries `search_terms`, `problem_types` and constraints. It is the unit that is searched, fit-evaluated and decided. `fit_evaluation`, `recommendation` and `build_authorization` are reused unchanged because they reference `project_requirement`.

`requirement_need_link` is N:M. One need can serve several requirements; one requirement can need several capabilities.

Needs come from the architecture sketch, so the order is: requirements, then sketch, then needs, then confirmation, then search. A requirement with no disposition blocks confirmation.

**The confirmed scope includes what will be searched for.** `search_terms` and `problem_types` are in the content the human confirms and in its hash. They decide what the registry is asked, so changing them after confirmation re-opens the gate.

**What is confirmed has to be what is asked.** A search term is plain ASCII letters, digits, single spaces and `+ # . - /` (`c++`, `node.js`, `ci/cd`). Anything else is refused, for one of three reasons:

- *it reads as something else.* Terms are listed with commas, so the single term `oauth, oidc, sso` displayed exactly like three terms, matched nothing, and produced a clean cold-start BUILD. Each term is now also shown in quotes, with a count;
- *it cannot be told from another term by looking.* A variation selector or zero-width character inside `oauth` changes nothing on screen and everything in a string comparison. So does a Cyrillic "о". Refusing a word that *mixes* alphabets was not enough: a word written *entirely* in look-alikes passed (`сасһе` in Cyrillic, `oauth` in mathematical or fullwidth letters, `ɡraphql` with a script g, `oıdc` with a dotless i), the human confirmed "cache", the registry was asked five Cyrillic letters, and "nothing in the registry" was recorded as CIP's own finding. No list of look-alikes is complete, so the rule is the alphabet: ASCII, which is what the registry's names and keys are written in. A refused term is reported with its code points spelled out;
- *it is a wildcard.* `%` and `_` mean "anything" to the registry's `LIKE` search.

A term has one spelling: runs of spaces become one space, and it is stored in lower case. The search is not case-sensitive, so case carried nothing except the chance to pass a capital I for a small l; and two spaces were displayed as one and matched neither.

The cost is stated in §12: a term in another alphabet is refused, so the mandatory sweep cannot match a description written in one.

**A license allowlist holds SPDX identifiers.** Each entry is trimmed and must be an identifier (`MIT`, `Apache-2.0`, `BSD-3-Clause`: letters, digits, `.`, `-`, `+`, no spaces). It is compared as written with the identifier read from a candidate's source, so `MIT License` or `Apache 2.0` matched no license at all: every candidate CIP had read a license for failed the allowlist with a confirmed gap, and the BUILD that followed was recorded as CIP's own decision.

**It also includes where each quote sits.** The same words can occur twice in a PDR, and moving a requirement's anchor changes which section looks read. The span is in the hash.

**A quote is one sentence in one place.** At most 600 characters, and it may not run across a heading into another section. Without those two limits a single "requirement" quoting the whole document marked every section as mapped.

`bespoke` is the obvious way to dodge a search. It needs a reason, every bespoke requirement is listed in what the human confirms, and it is listed again at lock time.

**Slugs.** An explicit `slug` updates that requirement. Without one the slug is derived from the text, and a derived slug that already exists is refused, whatever the existing row says: with no slug given, nothing says an update was meant. The same slug twice in one call is refused, and so is an explicit slug over 80 characters (two long slugs used to be cut to the same 80 and overwrite each other). The same rules hold for needs.

**Rewriting a requirement resets its passes, and keeps its fails.** If its text, kind or quote changes, every verification of its criteria that is not a fail is deleted, so a pass recorded against the old wording does not carry over to the new. A fail stays for as long as its criterion's own text does: something was run and did not pass, and rewording the requirement does not make that untrue. (The criteria rows themselves used to be replaced, fails and all. Change the text and change it back: the confirmed hash returns, no human is involved, and a failure CIP had observed itself was gone.) Evidence that does go, with a pass on a rewrite or with a criterion whose text changed, is written to the audit trail first (`verification_evidence_dropped`: criterion, outcome, who recorded it, command, commit, task).

**Stored text is one line.** Requirement text, reasons, criteria and need descriptions are stored with every run of whitespace, line breaks included, as a single space. They are printed later in rows of their own (`still unverified: <criterion>`), and a line break inside one starts a row that reads as CIP's. The quote is the exception: it has to match the PDR as written, and is shown through the one-line filter.

**An anchor is checked against the text it points into, every time it is read.** A stored quote has to be what the stored PDR says at the stored offsets, be no longer than the ceiling, and sit inside one section of the *current* text. An anchor that fails blocks confirmation until the requirement is re-recorded. This is what covers the two ways a bad anchor used to arrive without passing `record_requirements`: a row migrated from v1.0, whose offsets and quote the caller made up; and a requirement recorded against a flattened copy of the PDR and then carried onto the real one, where its quote spanned five sections. Carry-forward on re-ingest applies the same rules and orphans what fails.

**The legacy `import_requirements` tool is refused for a project that has a PDR.** It predates this workflow and writes the same rows with none of the rules: no quote, no limits, no reset of evidence, constraints on rows where nothing hashes or shows them.

**A field made of nothing is empty.** Zero-width characters, variation selectors, blank glyphs (U+2800, the Hangul fillers), whitespace, and combining marks with no letter to sit on do not make a reason, a criterion or a requirement: a field has to contain at least one letter, digit, punctuation mark or symbol. Requirement, need and lock fields are stored in NFC, so one name has one spelling.

---

## 3. Decision authority

v1.0 said CIP decides, then had the LLM pass in the verdict.

**CIP computes first, always**, through the Step 12 rules profile over the candidates the need's searches evaluated. `workers.recommend` was split into `preview_recommendation` (compute, no write) and `persist_recommendation`, so a decision that ends up refused does not replace the recommendation row and cascade away its build authorisation.

**Whether the computed verdict means anything depends on two things.**

*The need.* `core.project.fit` scores a candidate against constraints. With no machine-checkable constraint every candidate scores fit 1.0, and the rules would decide on text match and intrinsic score alone.

*The candidate.* The fit matchers read evidence. A component ingested from registry metadata alone has none, and that is most of the registry (the GitHub-search and awesome-list ingests set `capability.license_spdx` and write no `evidence_item`). Against such a candidate a license allowlist reports "no identified license" as a blocking gap, a required interface is always missing, and a forbidden dependency is always absent. Those are statements about missing evidence. The rules would turn them into REJECT, and so into a BUILD that CIP never had grounds for.

Evidence is judged **per constraint kind**, not as "was this component analysed at all". A candidate whose LICENSE file was read still has nothing to say about a required interface.

| Constraint | Evidence it needs |
|---|---|
| `license_allowlist` | a license CIP **identified** on the pinned revision. The extractor also writes a row for a LICENSE file it could not name (`spdx_id: unknown`) and a low-confidence one when a file only mentions a license; neither is a license CIP read. "Could not tell" used to become "read it, not allowed", and so BUILD by rule |
| `required_interface` | interfaces extracted from the pinned revision, or a succeeded interface-extraction stage on it that found none |
| `forbidden_dependency` | a manifest of the pinned revision was read: declared dependencies, dependency evidence, or a succeeded `manifests` analysis stage |
| any | a scorecard. Without one the intrinsic score is 0, which the rules read as a finding |

Evidence is read for **the revision that is pinned**, and for no other: license, interfaces and dependencies alike. A capability version can be bound to several revisions; what was read on an older one says nothing about the one that would be installed. Interface and dependency rows hang off the capability *version*, so they are counted through the evidence item they were extracted from, which belongs to one revision. There is one loader for a candidate's evidence, the Step 12 worker's; the search and the decision both use it. (There were two, and the rule reached one of them.)

A decision made earlier keeps its own pin. When the lock is built, each reuse decision's license is read from the revision *that decision* pinned (revision key and source asset), so the summary's "pinned: R1" and the license beside it describe the same revision even if a newer one has been bound since. The facts are looked up per decision, not per capability version: two needs can settle on one capability at different times and pin different revisions of it, and looked up per version one of the two lock lines printed the other revision's license.

**"CIP holds the evidence" means the matcher was given it.** What counts as held and what the fit matcher reads are the same rows, from one definition each:

- a license is one the extractor identified: not `unknown`, not `NOASSERTION`, not a low-confidence mention. The same test is applied where the matcher loads licenses. It used to be applied only to what CIP *reported*: an identified GPL-2.0 beside a file guessed to be BSD-2-Clause passed an allowlist with BSD-2-Clause on it;
- interfaces and dependencies are the evidence items on the pinned revision, whether or not promotion linked them to the version. Promotion is a no-op for a revision that already has a source binding, and every metadata ingest creates the binding first, so a component found by search and analysed afterwards had all of its evidence unlinked. Its analysis stages had succeeded, so CIP called the evidence sufficient; the matcher read the link tables, found an empty list, saw no forbidden dependency in it, and CIP adopted a component whose manifest named one. The matcher now reads both, mapping an unlinked row exactly as promotion would.

Search results mark each candidate `evidence: sufficient | insufficient` and list `missing_evidence`. A candidate's blocking gaps are split the same way: `confirmed_blocking_gaps` are the ones that rest on evidence CIP holds (a license it read and that is not allowed); the rest only record that the evidence is missing.

**When CIP's computed verdict carries its authority:**

| Computed | CIP stands behind it when |
|---|---|
| a reuse verdict | the need has a checkable constraint, and CIP holds the evidence for the candidate it chose |
| BUILD, with candidates | it holds the evidence for **every** candidate, the sweep saw all of them, and the rules accept **none** of them. "Nothing fits" is a claim about all of them: one readable GPL library ranked above three unread MIT ones is not grounds for it. Nor is a rejected rank 1: Step 12 runs the rules on the best-ranked candidate only, and ranks on fit before intrinsic score, so a perfect fit with a poor score is rejected while the candidate under it would have been ADAPT. The rules are now run over every candidate; if any earns a reuse verdict, BUILD is not CIP's to claim |
| BUILD, with no candidates | the sweep was not truncated |

Each `decision_record` is labelled with who actually decided:

| `decided_by` | When | Requires |
|---|---|---|
| `cip_rules` | CIP's verdict carried authority (table above) and the proposal agreed or was omitted | nothing |
| `llm_override` | CIP's verdict carried authority and the LLM proposes something else | rationale; listed at lock |
| `llm_judgment` | it did not: no checkable constraint, missing evidence, a truncated sweep, or rules that reject the best-ranked candidate and would accept another. The decision carries which (`judgment:no_checkable_constraint`, `judgment:insufficient_evidence`, `judgment:sweep_truncated`, `judgment:rules_accept_another_candidate`) and the lock summary gives that reason, not a generic one | proposal and rationale; listed at lock |

In the last case the agent may well choose the candidate the rules accept, and the decision is still labelled LLM judgment. That is the cautious label, not the precise one: CIP's own algorithm picked a different candidate, so it does not sign the choice. A BUILD over a candidate the rules accept is flagged `build_over_acceptable_candidate`, whoever decided it.

Other rules:

- A reuse verdict must name a capability version that a search for this need evaluated, and that version must have a source binding so it can be pinned to a revision.
- **REJECT is not an outcome for a need.** It is a statement about candidates. A need whose candidates are all rejected still has to be satisfied, so a computed REJECT becomes BUILD. A need nobody wants is retired (`retire_requirement`).
- Choosing a candidate that has a blocking fit gap on real evidence is allowed and flagged `overrides_blocking_gap`.
- Choosing a candidate CIP lacks some evidence for is flagged `unanalysed_candidate`: that part was not verified from source. Running Step 6 analysis on the repo is what changes that.
- The two flags are independent, and a candidate can carry both. (They were once exclusive, so adding an uncheckable interface constraint to a need turned a GPL finding into "unanalysed".)
- The recommendation row and the decision record are written in one transaction. A failure between them used to leave the recommendation replaced, its build authorisation gone, and no decision.
- `recommendation` stays the single mutable "current verdict" row Step 13 reads. Until the lock it holds what CIP computed. An approved override or LLM judgment becomes effective when the lock is written.
- `decision_record` is append-only history, ordered by an identity column. The lock references `decision_record` ids, which survive re-recommendation.

### Cold start, and the mandatory sweep

v1.0 allowed BUILD only if a `fit_evaluation` row existed. A search that found nothing wrote no row, so on an empty registry BUILD was refused forever, unless the query was widened until a junk candidate appeared.

`requirement_search` logs every search, including an empty one. That row is the proof a search ran. A decision requires the **mandatory sweep**: the need's own confirmed `search_terms`, unfiltered, against the need as currently defined. A custom query or a filtered search adds evidence and never substitutes.

"As currently defined" is the `need_fingerprint`: search terms, constraints and problem-type tags. The tags are what the evaluation store is recalled by, so a sweep run before a tag was added never looked for the banked assets that tag would have found.

The sweep cannot be narrowed from the call:

- an empty-string filter is no filter (it used to filter on `''`, match nothing, and still be logged as the unfiltered sweep);
- `limit` does not apply to it. The sweep takes up to **100 hits per term** (it was 20, silently, and the candidate that fit could be the twenty-first);
- the terms are the confirmed ones;
- the project's policy constraints do not filter it. What they exclude is still found, counted (`policy_excluded_count`) and listed under `unevaluable` with the reason.

And it says when it did not see everything:

- a term that returns 100 hits sets `truncated`. CIP will not conclude "nothing fits" from a truncated sweep, and a BUILD decided over one is flagged `sweep_truncated`;
- the decision rests on the latest sweep, and that sweep has to describe what it read as it now is. Each search stores a fingerprint: how many capabilities, versions, source bindings, revisions, evidence items, scorecards, interface and dependency rows and finished analysis stages there are, the content of the capability rows, which versions are superseded and in what lifecycle state, and the evaluation store (a count and a checksum of its rows). If it has moved, `record_decision` is refused until the sweep is run again. (A count of capabilities alone missed the event the workflow asks for most: the match the sweep could not evaluate being ingested and analysed. And the evaluation store, the half of the sweep that says "something like this was assessed before", was not in the fingerprint at all: an evaluation banked after the sweep left the BUILD recorded as a cold start.)

Each decision records a `coverage_outcome`:

| Outcome | Meaning |
|---|---|
| `candidates_evaluated` | at least one registry candidate was fit-evaluated |
| `found_but_unevaluable` | the registry returned matches and none could be evaluated (no source binding to pin, or excluded by project policy). Not the same as having nothing: ingesting them would make them candidates |
| `banked_only` | nothing in the registry; the evaluation store has a tag-matched banked asset |
| `no_registry_coverage` | CIP had nothing. The BUILD was not chosen over reuse; the question was not answerable |

A BUILD with no registry coverage is flagged `cold_start_build`, and the flag shows the terms that were searched. A BUILD over unevaluable matches is flagged `unevaluable_candidates`. A BUILD despite a banked match is flagged `build_despite_banked_match`.

The evaluation store upserts by slug, so the way round that last flag was to overwrite the banked evaluation first. Every overwrite that changes an evaluation's verdict, banked state, problem types or evidence depth now writes an `evaluation_replaced` audit event with before and after, and a BUILD on a need whose tags matched an evaluation that was then rewritten into one that no longer matches is flagged `banked_evaluation_changed`. "Matched" is the search's own test (a shared tag, and a verdict other than Reject or the banked flag), and the window opens 30 days before the project was created, so an overwrite made in preparation is seen too.

A need with no `problem_types` cannot be matched to a banked evaluation by tag at all. The confirmation summary says so under that need, and a search that finds text-only matches in the evaluation store says they are there.

`coverage` reports `registry_answered_pct`, so a project where every search was empty does not look like a clean record of considered BUILD decisions.

---

## 4. Human gates

v1.0 had two gates "marked" nowhere: no column, no table, no tool. `approved_by` was free text supplied by the LLM calling the tool, so the governed party signed its own approval.

There are two gates, each with state:

| Gate | Opens | Blocks until approved |
|---|---|---|
| Requirements confirmation | `request_requirements_confirmation` | `search_for_requirement`, `record_decision`, `lock_architecture` |
| Architecture lock | first call to `lock_architecture` | the lock itself, and therefore every build task |

**An approval is bound to content.** `approval_request` stores the exact subject shown to the human and its canonical hash. A gate passes only when an approval exists for the hash of the *current* content. Edit a requirement after confirmation, or change a provider after lock approval, and the hash moves: the approval no longer applies.

**An approval is also bound to history.** Content alone is not enough: the same content can come round again in a different situation, and an old approval must not answer for it.

- A confirmation request records the approval it follows (`follows_approval_id`). The scope is confirmed only when the *latest* approved confirmation is for the current content. Confirm A, edit to B and confirm B, edit back to A: A needs confirming again.
- A lock payload records the lock version it replaces (`replaces_lock_version`), inside the hash. v1 approved with a provider, v2 approved without it: asking for v1's inputs again is a new request against v2, not a replay of v1's approval.
- A pending request that no longer corresponds to anything is **obsolete**: another confirmation was approved since it was made, the lock it diffs against is no longer the active one, the requirements were edited after it was requested, or a later lock request has been made. It cannot be approved and the CLI says why. One function decides this for `pdr_status`, `approvals list` and the approve prompt, so they cannot disagree about what is waiting.
- **One live lock request per project**: the one asked for most recently. Otherwise every variation an agent tries stays approvable for good, and the person at the terminal has to work out which is meant.
- **Only the latest request can become the lock, approved or not.** Approve request A; the human changes their mind, and B is requested and approved. A's approval is still on record, and it is no longer their latest word: writing A is refused. Asking for A's content again does not revive it either. A lock payload records the request it follows (`follows_lock_request`, inside the hash), so the same content asked for after B is a different payload, a new request, and a new approval. Asking twice in a row for the same content is still the same request.
- A pending lock request goes obsolete when the project moves under it (a decision, the requirements, the scope): replayed, it would no longer give the content it describes.
- **A lock approval is for one confirmation of the scope.** The payload names the requirements confirmation in force when the lock was requested (`requirements_approval_id`, inside the hash). Confirm scope A and have a lock approved; confirm B; go back to A and confirm it again. The requirements hash is A's throughout, and the old lock approval was written. It no longer matches: the lock for the re-confirmed scope is asked for again. A lock that was already *written* under the first confirmation is not reopened by the round trip: the confirmation is history, not content, and the scope it was approved for is the confirmed scope again.
- **"Latest" is the order requests were written in**, read from an identity column on the audit trail, not from a timestamp. The timestamp is when the writing transaction started: a call that began first and finished last was the newest request with the oldest time, was reported as superseded, and asking for it again returned the same request, which nobody could approve. Lock calls for one project also run one at a time (a per-project advisory lock, held for the transaction), and the write looks once more for a later request before it commits.
- An approval writes one lock. `architecture_lock.approval_id` is unique.

**Nothing in the MCP server writes an approval.** The only writer is `cip-composer approvals approve|reject`, which:

1. refuses unless stdin and stdout are an interactive terminal, and treats end-of-input at the prompt as "nobody there" (on Windows the NUL device reports itself as a terminal);
2. prints the full summary of what is being approved, and whether the file behind it still matches what was read: the PDR for a requirements confirmation, the architecture document for a lock. (Only the PDR was checked; an architecture document swapped after the request was approved under the old hash without a word.)
3. refuses an obsolete request;
4. requires the human to type the first six characters of the content hash;
5. records the OS user and host as the approver, with channel `cli_tty`. The user comes from the operating system (`os.getlogin()`), not from `USER` / `USERNAME`, which anything in the environment can set;
6. after the code is typed, looks at the request again, with the same rule that decides what is waiting: the summary was on screen for as long as it took to read, and a decision or the scope may have moved under it. If it did, nothing is recorded;
7. re-checks that the stored subject still hashes to its `subject_hash`.

A request is looked up by its UUID or a hex prefix of at least six characters that matches exactly one request. (The first version used `LIKE`, so `approvals approve %` matched whatever was pending.)

One decision per request, ever. To change your mind, change the content, which opens a new request.

**One approval covers the whole lock**, including every override and LLM judgment in it. They are not separately approved; they are what the summary puts first.

**The approval note is the approver's.** `lock_architecture` takes no approver and no note. The note stored on the lock is whatever the human typed when approving, kept as one clean line like everything else that is shown again later. v1.1's first cut still accepted an `approval_note` from the caller, which let an agent store "approved verbally" on the lock.

### What the human is shown

The summaries are printed in a terminal just before a person types a confirmation code, and much of their text comes from the agent whose work is being approved. So:

- **Every field is rendered as one logical line of visible characters** (`core.pdr.text.one_line`): control characters, escape sequences, bidi and zero-width characters, line separators and blank glyphs are replaced. An agent-written "risk" cannot erase the warning lines above it or start a line of its own.
- **CIP wraps the lines, not the terminal.** A field kept to one logical line can still be padded until the terminal wraps it, and whatever lands at the start of the wrapped row reads as a line CIP wrote. So the CLI hard-wraps every summary to the terminal's width and starts each continuation row with `    | `. A forged row can only ever appear behind that mark. Rows are measured in terminal columns, not characters, and every doubtful character is counted as two (fullwidth and CJK, emoji, and the "ambiguous" class that an East Asian terminal draws wide): wrapping early costs nothing, and a field padded with fullwidth letters otherwise has half the characters the wrap allowed for and twice the columns. The width is asked of the terminal itself, never read from `COLUMNS`, and the narrowest attached terminal wins, so output piped into a pager is wrapped for the screen the pager draws on. With no terminal attached it wraps for 80 columns.
- **Everything the CLI prints goes through one filter** (`_SafeStream`) that removes control characters and, for every command a person reads to decide something, wraps and marks every line, not only the summary. A summary was wrapped; the `Project: <name>` line under it was not, and a padded name put a forged `PDR file: ... matches` row on the approval screen. JSON output (`--json`) is for a program and is not wrapped.
- **Names are short single lines.** A project name is at most 60 characters, a PDR title or task title 120, each one plain line; refused otherwise.
- **A file a CIP tool wrote is said to be one.** `scaffold_pipeline`, an older tool on the same server, writes a README with text the caller supplies. Ingested as the PDR it was shown with a path, a SHA-256 and "on disk matches", like a document a person provided. Every file that tool writes is now logged with its hash, and a PDR whose bytes match one is labelled in the summary as written by a CIP tool at an agent's request.
- **The lock summary renders the whole payload.** Under each decision: what it pins (source and revision), the license and where it was read (from source, or registry metadata), the cost tier, what the registry was able to say, and the terms that were searched. For a decision CIP did not make: what CIP computed, or that it computed nothing it would stand behind, and the agent's stated reason. An override used to reach the human as a flag with neither side of the disagreement. Then cross-cutting decisions, providers, cost commitments, risks, scope, the architecture document, hashes, and what the lock replaces. Nothing that is hashed is left unshown.
- **The requirements summary identifies the document**: title, file path and SHA-256 of the file as ingested. Text passed as `pdr_text` has no file behind it and is labelled "supplied inline by the agent". If any other text has been this project's PDR, the summary says so.
- **A second confirmation shows what moved.** "Changes since the scope you last confirmed" comes first: requirements and needs added, removed and changed, field by field (priority, disposition, criteria, search terms, constraints, where the quote sits). Retired items are listed with their reasons. On a long list that looks like the last one, a dropped must-have or a removed license constraint is otherwise approved unseen.
- **Quotes are shown in full**, which the 600-character ceiling makes possible. They were cut at 200.
- **The lock summary names the architecture document**: the file path and the SHA-256 of the file as read, or that the text was supplied inline. A bare content hash gave the human nothing to check.
- **The lock summary ends with where the request sits in history**: the requirements hash, the lock it replaces, the confirmation it was made under and the lock request it follows. All four are hashed, so all four are shown.
- **Stored values are printed as one line each** by every command a person reads, including the three that print outside a summary: the search screen (a registry display name comes from the internet), the lock check, and `verify`.
- CIP's own words and punctuation in a summary are ASCII, so a cp1252 console can print the frame. The agent's text is shown as written, minus the characters above, so a non-ASCII letter appears as itself (or as `?` on a console that cannot draw it).

**The legacy `authorize_build` tool** (Phase 10) takes a free-text actor. It now refuses a recommendation that belongs to a capability need. Those are authorised by the lock, in the approver's name.

---

## 5. Schema — migration 030

Additive. Legacy rows keep working; new columns are nullable or defaulted.

| Object | Change |
|---|---|
| `requirement_source` | `raw_text` (the PDR is stored), `supersedes_id`, `source_file_hash` (sha256 of the file's bytes), `activated_at` (the most recently activated source is the current PDR) |
| `project_requirement` | `kind`, `disposition`, `disposition_reason`, `search_terms`, `problem_types`, `status`, `retired_reason`; trigger: a row never crosses the requirement/need boundary |
| `requirement_need_link` | new, N:M; trigger checks kinds and same project |
| `acceptance_criterion` | new, one row per criterion; backfilled from the v1.0 text column |
| `requirement_search` | new, append-only search log; `truncated` (a term hit the ceiling), `policy_excluded_count`, `registry_size` and `registry_stamp` (a fingerprint of what the search read) at the time |
| `decision_record` | new, append-only decision log, `seq` identity for order; `coverage_outcome` is one of four values, `found_but_unevaluable` among them |
| `approval_request`, `approval` | new, append-only |
| `audit_event` | new, append-only, `seq` identity for order. v1.0 assumed this table existed; it did not |
| `architecture_lock` | `approval_id` (unique), `payload_hash`, `requirements_hash`, `architecture_doc_hash`, `architecture_doc_ref`, `architecture_doc_file_hash`, `project_decisions`, `scope`, `replaces_version`, `follows_request_id`, `requirements_approval_id`; whole-row immutability trigger replaces the v1.0 rule that guarded `decisions` only |
| `requirement_verification` | `acceptance_criterion_id`, `commit_sha`, `test_ref`, `command`, `exit_code` (BIGINT), `output_digest`, `worktree_dirty`, `recorded_by`, `legacy`, `recorded_seq` identity; CHECK: a `pass` carries its evidence unless the row is `legacy` |
| `evaluation` | `evidence_depth`; `Wrap` joins the verdict set; CHECK: a grade requires `code_read` |

House style: append-only tables use the same `ON UPDATE ... DO INSTEAD NOTHING` rule as `build_authorization`. Two places need conditional logic a rule cannot raise on, so they use triggers that RAISE: lock immutability and link consistency.

Things learned building it, worth keeping:

- Postgres refuses `INSERT ... ON CONFLICT` on a table that has an UPDATE rule. `approval_request` is written with SELECT-then-INSERT; the UNIQUE constraint backstops a race.
- A no-update rule swallows the internal UPDATE of an `ON DELETE SET NULL` foreign key, which then fails referential integrity. `decision_record.recommendation_id` is therefore a plain UUID, not a foreign key: `recommendation` rows are deleted and re-inserted on every re-run.
- A `NOT VALID` CHECK is still re-checked whenever a row is updated. Deleting a v1.0 task sets `build_task_id` NULL on its verification rows, which re-ran the pass-evidence CHECK against rows that predate it, and the delete failed. Rows written before 030 are marked `legacy` and exempt by value. They carry no criterion, so they never count as verification.
- `now()` is constant inside a transaction, and it is the transaction's start. Several verifications reported in one call shared a timestamp, so "latest" was arbitrary; and of two overlapping calls, the one that finished last could carry the earlier time. Order comes from identity columns (`recorded_seq`, `decision_record.seq`, `audit_event.seq`), and timestamps that matter use `clock_timestamp()`.

**030 has been edited in place** during this work, most recently to add `audit_event.seq` and `architecture_lock.requirements_approval_id`. That is legitimate only because 030 has never been released: no database outside development has applied it. The migration runner tracks checksums, so a development database that applied an earlier 030 has to be dropped and recreated. Once 030 ships, any further change is a new migration.
- `exit_code` is BIGINT: a Windows crash code such as 0xC0000005 does not fit 32 bits.

---

## 6. Tool contracts

Thirteen MCP tools make up the workflow. Return conventions are v1.0's: `None` for an unknown id, `{"error"}` for bad input, `{"refused": true, "reason"}` when a gate says no.

They are not the only tools on the server, and the others matter because an agent holds them too. Those that write into what the gates read are held to the same rules: `import_requirements` is refused for a project with a PDR (§2); `authorize_build` refuses a capability need (§4); `scaffold_pipeline` writes files, which are logged (also when it fails halfway) and labelled if one is ingested as a PDR (§4); `record_evaluation` leaves an audit event when it changes an evaluation the searches recall (§3).

Bad input is an answer, never an exception: a NUL, a lone surrogate, NaN, a wrong type or an over-long value returns `{"error"}`. So does an id that is not a UUID written the usual way (braces, `urn:`, no hyphens), a `limit` of infinity, and a legacy row in a shape this version does not write. Two sessions racing for the same lock or the same request get `{"error", "retry": true}` from the loser, with its transaction rolled back.

Everything the agent writes that is stored or shown has a ceiling:

| Field | Limit |
|---|---|
| requirement text, reason, criterion, need description | 500 characters |
| quote | 600 characters, one section |
| slug | 80 characters |
| project name; PDR title; task title | 60; 120; 120 characters, one plain line |
| criteria per requirement | 20 |
| search terms per need; problem types | 8 (2 to 60 ASCII characters each); 12 |
| license allowlist entry | an SPDX identifier, no spaces |
| one constraint's detail | 1,000 characters |
| rationale | 2,000 characters |
| requirements or needs in one call | 500 |
| lock names; lock text fields; items per lock list | 80; 300; 50 |
| `check_against_lock` change | 20,000 characters (larger is `blocked`, and is not stored) |
| verification reports in one call | 100 |
| `test_ref`, `evidence_ref`; `command`; `output_digest` | 500; 2,000; 128 |
| permitted paths per task | 100 |
| a document | 2 MB |

| Tool | Writes | Contract |
|---|---|---|
| `pdr_status(project_id \| project_name)` | no | Phase, ids, blockers, pending approvals, an approved lock request waiting to be written, the active lock and everything it froze, tasks, `next_actions`. First call of every session. |
| `ingest_pdr(project_name, pdr_title, pdr_path \| pdr_text)` | yes | `pdr_path` must be absolute and a text document (`.md .markdown .txt .rst .text`); UTF-8 or UTF-16 with a BOM. Stores normalised text (BOM stripped, CRLF folded) and the file's SHA-256. A new text becomes current; quotes still present are carried forward, the rest reported `orphaned`. Ingesting an earlier text again makes it current again. |
| `record_requirements(project_id, requirement_source_id, requirements[])` | yes | Each needs `text`, a verbatim `quote`, `acceptance_criteria[]`. CIP computes the span. Returns `span_coverage.unmapped_sections`. |
| `record_capability_needs(project_id, needs[])` | yes | `description`, `search_terms[1-8]`, `problem_types[]`, `satisfies[]`, `constraints[]`. Warns when a need has no checkable constraint. |
| `retire_requirement(req_id, reason)` | yes | Takes a requirement or need out of scope, on the record. |
| `request_requirements_confirmation(project_id)` | request only | `blocked` with blockers, or opens the approval request. Returns `changes_since_last_confirmation`. |
| `search_for_requirement(req_id, search_query?, filters?, limit?)` | log + fits | Need only. Returns `candidates` (each with `evidence`, `missing_evidence`, `confirmed_blocking_gaps`, `license_source`), `unevaluable`, `evaluation_matches`, `coverage_outcome`, `counts_as_mandatory_sweep`, `truncated`, `policy_excluded_count`. |
| `record_decision(req_id, verdict?, chosen_capability_version_id?, rationale?)` | yes | `verdict` is the proposal. Returns `decided_by`, `computed_verdict`, `flags`. |
| `pdr_approval_brief(project_id, <lock inputs>)` | no | Exactly what the lock would freeze. Same function as the lock. |
| `lock_architecture(project_id, <lock inputs> \| approval_request_id)` | yes | First call, with the inputs, opens the request. Second call, with `approval_request_id` alone, replays exactly what was approved and writes the lock. No approver argument, no note argument. Returns `tasks_abandoned`: the tasks the new lock does not carry (§8). |
| `check_against_lock(project_id, proposed_change)` | audit row | Structured input. `allowed`, `needs_approval` or `blocked`, with the locked items cited. |
| `record_build_progress(...)` | yes | Create is the build gate. Update moves status and records verification. |
| `pdr_coverage(project_id)` | no | Section 10. |

Lock inputs: `architecture_doc_path | architecture_doc_text`, `approved_providers[]`, `cost_commitments[]`, `project_decisions[]`, `unresolved_risks[]`.

**Document paths are absolute.** The MCP server's working directory is CIP's own folder, so a relative path resolved to a file there, not in the project the agent was working on. The `cip-composer` CLI resolves a relative path from where the person is standing before calling the tool.

A path is absolute by the rules of the machine the server runs on. On Windows that means a drive or a UNC share: `/Users/x/pdr.md` has neither and is refused there.

**The document reader reads documents.** `ingest_pdr(pdr_path=...)` would otherwise read any file the server can and store it where the agent can read it back. It accepts the five text suffixes only, up to 2 MB. Set `CIP_DOCUMENT_ROOTS` (path-separator list) to confine it to named folders; this is recommended on any machine that holds text files the agent should not read. If the variable is set and names no folder, nothing can be read: a blank value used to mean no confinement.

Removed from v1.0: `evidence_refs` on `record_decision` (it was accepted and ignored; fit evidence is linked by CIP), and the caller-supplied approver and note on `lock_architecture`.

Human-only CLI, not exposed as tools: `cip-composer approvals list|show|approve|reject`, `cip-composer verify`.

---

## 7. The lock

### What is hashed

`payload_hash` is the sha256 of the canonical payload:

- `requirements_hash` (the confirmed scope);
- `architecture_doc_hash`, if a document was supplied, with where it came from (`architecture_doc_ref`: a path, or `inline`) and the SHA-256 of the file's bytes (`architecture_doc_file_hash`), which is the figure a person can check with their own tools;
- one entry per capability need: `decision_record_id`, verdict, capability version, pinned source and revision, `decided_by`, `coverage_outcome`, flags, license and where it was read (`license_source`), cost tier, the terms that were searched, the requirements it serves, what CIP computed, and the agent's rationale;
- `project_decisions`: cross-cutting choices that belong to no single requirement (database, hosting, auth model);
- `approved_providers` and `cost_commitments`;
- `unresolved_risks`;
- `scope`: what is in scope, bespoke, deferred, not applicable;
- `replaces_lock_version`: the lock this one replaces, or null for the first;
- `follows_lock_request`: the lock request that was the latest one when this payload's request was made, or null for a project's first (§4);
- `requirements_approval_id`: the confirmation of the scope the lock was requested under (§4).

Canonical JSON (`core.pdr.canonical`): sorted keys, no insignificant whitespace, UTF-8, no floats. Stable under `PYTHONHASHSEED=random` across processes, tested the same way as `scorecard.computed_hash`. `source_lock_hash` remains the hash of the decisions alone.

Whether a relock changes anything is judged on the content without the three history fields (`content_core_hash`). A relock with identical content needs no approval and writes no new version.

`rejected_alternatives` is stored on the lock for the record and is not part of the hash.

### What needs the human's attention

Derived from structured facts, most serious first. v1.0 matched words like "payment" against requirement text.

`overrides_blocking_gap`, `llm_override`, `non_permissive_license`, `unknown_license`, `unanalysed_candidate`, `sensitive_provider` (ai, auth, payment), `data_leaves_infra`, `paid_provider`, `paid_capability`, `cost_commitment`, `build_over_acceptable_candidate`, `build_despite_banked_match`, `banked_evaluation_changed`, `unevaluable_candidates`, `sweep_truncated`, `cold_start_build`, `llm_judgment`, `unresolved_risk`, `bespoke_requirements`, `no_architecture_doc`.

Two of these rest on labels the agent supplies, so each has a floor under it:

- **Providers.** A provider's category is the agent's own. A short list of well-known ai, auth and payment providers is flagged `sensitive_provider` whatever category it was declared under ("Stripe" filed as `other`). Names are compared with everything but letters and digits removed, so `S t r i p e` and `STRIPE, Inc.` match; a name with an invisible character, or a word that mixes alphabets, is refused outright. A name with any letter or digit outside ASCII that is not declared sensitive is flagged `sensitive_provider` too, with the reason: the list is ASCII names, so `ОКТА` written in Cyrillic reads as Okta and matches nothing on it, and CIP cannot tell that from a provider whose name really is in another alphabet. It says it could not check, and the person reading decides. The list is a floor, not a registry: an unlisted ASCII provider is taken at its declared category.
- **Costs.** A commitment is flagged if its kind is not free **or** its amount holds any digit other than zero. `kind: free, amount: 20000` used to raise nothing. Two lines for the same item are refused, because that is how a large one hides behind a small one.

Flags are the attention list, not the disclosure. The summary shows every provider and every cost commitment whether or not it raised a flag.

### Writing the lock

In one transaction, under an approval for this exact payload, and only for the most recent lock request (§4):

1. each `recommendation` row is updated to the approved verdict. The marker Step 13 reads as proof that a search ran and evaluated nothing is kept whoever made the decision: an LLM-judged BUILD over a truncated, empty sweep was otherwise approved by the human and then refused by Step 13, for good;
2. the active lock is superseded and the new version inserted, carrying the approver's name and note;
3. a Step 13 `build_authorization` is created for every BUILD need, in the approver's name, replacing any authorisation that did not come from this approval;
4. any task stored as `verified` whose evidence no longer covers its criteria moves back to `implemented`;
5. every task the new lock does not carry is abandoned (§8), and the call returns their ids.

The second call normally passes only `approval_request_id`. CIP rebuilds the payload from the approved request's stored inputs and the project's current state; if the result no longer hashes to what was approved (a decision changed, the requirements moved, another lock was written), the replay is refused and a fresh request is needed. Passing the same inputs again also works.

The row is immutable. The only legal update is `active` to `superseded`.

### Change requests

There is no separate change-request entity. A change is: re-decide, then `lock_architecture`. The relock's approval request shows `changes_from_active_lock`. That diff is the change request, and approving it is approving the change. Because the request is tied to the version it diffs against, it cannot be approved once that version is no longer the active one.

A diff is only a change request if it is complete, so it is built to be:

- every decision is compared whole. A change to its verdict, capability, pinned source or revision, who decided, flags, license, license source, cost tier or coverage outcome is shown as before and after; a change to what it serves or what was searched is shown; any other field that differs is named;
- a decision recorded again with the same outcome is listed (`decision rerecorded`): the lock would point at a different decision record;
- providers, cost commitments and project decisions are compared item by item in full (a provider's purpose, a cost's amount, a decision's rationale), risks as added and removed, scope as what moved between in scope, bespoke, deferred and not applicable;
- the architecture document: changed content, or the same content read from a different file;
- anything left over, in a part of the payload no comparison reads, produces a line saying the content differs in a way not itemised;
- no line reads "X -> X". When two items differ in something their description does not show (an internal version id behind the same key and revision, the capitalisation of a name), the differing fields are named;
- if the active lock's stored content cannot be compared at all, the diff says that, and does not raise.

"None: the content is identical to the active lock" is printed only when it is. (The first cut compared verdict and capability only; a relock that swapped a license, a provider's data handling and a cost said "none".)

### Staleness

A lock is not relied on when:

- it has no recorded approval (a v1.0 lock);
- its stored decisions are not in a form this version reads (it used to raise, in every tool, for that project);
- its stored content no longer matches `payload_hash`;
- the requirements have changed since it was written;
- **the requirements are not confirmed as they now stand.**

The last is not implied by the third, and missing it was the one critical hole the second review found. Confirm scope A and lock it. Add a requirement and have the human confirm B. Retire the addition. The requirements hash is A's again, so the old lock's hash matches, but the human's latest word was B, and A has not been confirmed since. Gate 1 was closed and the build gate beside it stood open. A lock is usable only while the scope it was approved for is the confirmed scope.

A stale lock blocks new tasks, blocks progress and verification on existing ones (a task can still be abandoned), and makes `check_against_lock` return `needs_approval`.

Separately, a single need whose decision changed since the lock (**drift**) puts the work that rests on that need on hold and leaves the rest alone: no new task, no progress or verification on an existing one, and `needs_approval` from the lock check for that need's capability, its dependency, or a path of a task that rests on it, until a relock. The first cut applied this to new tasks only, and the second answered `allowed` for the capability unless a path was sent with it.

A new PDR text becomes the current source, re-anchors requirements whose quotes survive, and changes the requirements hash. So: reconfirm, then relock. The same holds for going back to an earlier text.

---

## 8. Build tasks

v1.0 gated recording a verdict. The moment that matters is creating a `build_task`, because that is when code starts. Step 13's `can_generate_for` guarded `generate_implementation`, which the PDR path never called.

`record_build_progress` in create mode refuses unless (`core.pdr.tasks.task_gate`):

- there is an active, usable lock;
- `permitted_paths` is a list of repo-relative paths with no `..`, each starting with a literal directory or file name. `src/**` is fine; `**`, `*` or `*/x` is refused, because a leading wildcard permits the whole repository;
- the task links at least one PDR requirement (what it delivers);
- every linked requirement is active and `via_needs` or `bespoke`;
- every capability need the task rests on, linked directly or through a requirement, is in the lock, and its current decision is the locked one;
- for a BUILD need, **`can_generate_for` says yes**;
- a `reused_capability_version_id`, if given, is one the lock pinned for a need the task rests on.

The reuse pin is normally left out. When the task rests on exactly one ADOPT / ADAPT / WRAP capability, CIP fills the version in from the lock and returns it; the answer is already determined, so nothing is gained by asking the agent to echo it. This holds whether the need is linked directly or reached through a requirement it serves (the first cut only checked directly linked needs, so linking the requirement alone skipped the pin). A task may rest on several reused capabilities, because one requirement can need several; then there is no single version to fill in and the column stays empty unless one is named.

REFERENCE needs are built under the lock approval alone. Step 13 authorises only BUILD recommendations, and that module was left as it is.

A task from an earlier lock version stays **current** if none of the decisions it rests on changed, everything it links is still an active row, and every requirement it delivers is still in the build. Otherwise it is stale and can only be abandoned. A task made under a v1.0 lock was never authorised by anyone; a later, approved lock does not adopt it.

**A lock ends the tasks it does not carry.** When a lock is written, every task that is not current under it is abandoned, with a `task_updated` audit event naming the lock. Abandoning used to be left to the agent, and a task that was never abandoned came back: defer its requirement and relock, bring the requirement back and relock again, and the task was current once more with the status and the passes it had two locks ago. Between an edit and the relock a stale task is still only held (the lock itself is stale then, and nothing moves).

A task row written before the rules on path patterns can hold `**`. Such a pattern now permits nothing, and the lock check says so.

Status moves `planned` → `in_progress` → `implemented` → `verified`, or to `abandoned`. An abandoned task is closed: it takes no further status and no verification. A task's title is one plain line of at most 120 characters; it comes back inside refusals and lock-check reasons.

A refused task operation writes a `task_refused` audit event and nothing else, with one exception: when `verified` is refused, the verification reports sent in the same call are kept. A report is evidence whether or not it earns the status.

---

## 9. Verification

v1.0: `test_result_id` and `evidence_ref` both nullable, no commit, no link to a criterion, and a task could be set to `verified` with no verification row at all.

- Acceptance criteria are rows. A verification points at one criterion.
- A `pass` must carry `commit_sha` (7–64 hex characters), `test_ref`, `command` and `exit_code` 0. Enforced in Python and by a CHECK constraint.
- A task can only verify criteria of requirements it links.
- **Latest wins**, by insertion order. A criterion's state is its most recent verification; within one call, the last report wins. One exception: `not_run` does not replace a fail. "I did not run it", filed after a failure, is not news about the failure, and it used to turn "failing" into "unverified". After a pass it does what it says: the criterion is unverified.
- `verified` requires a passing latest verification for every criterion of every linked requirement, and at least one recorded against this task.
- Nothing is recorded while the lock is stale. Otherwise a criterion could be rewritten to something easier and a pass reported against the new wording before any human had seen it.

**`verified` does not outlive its evidence.** The stored status is a flag, and the facts under it can move: a later fail from the same task; a fail recorded by another task on the same criterion; a criterion added; a criterion rewritten (its old evidence goes with the old row); the requirement itself rewritten; the task abandoned, or made stale by a relock; the requirement deferred or retired. In each case the task is no longer `verified`. Every read (`pdr_status`, `coverage`, the lock check) derives the effective status from the evidence and never takes the stored flag on trust.

**A pass stands while the work it was recorded against does. A fail stands.** A pass counts only while the task that recorded it is live: current, started, and not abandoned. (A pass recorded while a task was in progress does not count while the task sits back at `planned`.) A pass filed by a task that was then abandoned is evidence about work that is no longer the build. A fail counts whatever becomes of its task: something was run and did not pass, and abandoning the task that ran it does not make that untrue. (The second cut dropped all evidence from dead tasks, fails included. So: T0 passes; T1 observes a failure on the same criterion; abandon T1, and T0's old pass was the latest word again.) There is one rule and one query for it, used by the write that grants `verified` and by every read that reports it.

**A pass is a claim about work.** It is refused against a task that is still `planned`; a fail is recorded whatever the task's status.

**Verified is the end of a chain.** A requirement counts as verified when its decision is the locked one, a live task has reported it implemented, and every criterion's latest evidence is a pass. Each stage in `coverage` implies the one before. Without the chain a project read `complete` with every task still planned, and after a locked ADOPT had been re-decided as BUILD with no relock.

Editing a requirement keeps the row of any criterion whose text is unchanged, so its history survives. If the requirement's own text, kind or quote changed, the passes go and the fails stay (§2).

Two evidence sources, reported separately by `coverage`:

| `recorded_by` | How |
|---|---|
| `agent_reported` | the coding agent reported the outcome through `record_build_progress` |
| `cip_executed` | `cip-composer verify --task T --req R --criterion N -- <command>` ran the command without a shell in the target repo and recorded the exit code, a digest of the raw output bytes, `git rev-parse HEAD` and whether the tree was dirty |

**What `cip_executed` proves is narrow:** this command exited 0 here, at this commit. It does not prove the command tests the criterion; `verify -- python -c pass` exits 0. So:

- a pass on a dirty working tree is recorded but does **not** count as CIP-executed in coverage, because the code that ran is not the code at the commit. The tree is checked before the command and again after it, so a command that edits the tree does not get a clean record;
- the repository's root is stored with the run (`evidence_ref`), returned by `coverage` with each criterion's evidence and printed by `cip-composer pdr-coverage` beside the command, so a pass recorded in some other repository can be seen for what it is. CIP does not know which repository is the project's; a person has to look;
- `coverage` returns the command, test reference and commit for every criterion, so a person can read what was actually run.

`verify` resolves the program on `PATH` the way a shell would (so a `.cmd` shim such as `npm` is found on Windows) without using a shell, and hashes output as bytes (decoding it first lost the run whenever a test printed a character the console code page could not represent).

`verify` is a CLI command and deliberately not an MCP tool. A tool that runs arbitrary commands would hand a shell to any client connected to the server.

---

## 10. Coverage

v1.0 divided by the number of extracted requirements. A PDR that was 70% extracted reported 100%.

`coverage` reports its denominators:

- **pdr**: sections of the stored text with no requirement mapped (`unmapped_sections`; an anchor that is blocking confirmation because it is too long or crosses a heading maps nothing until it is fixed); sections quoted only by deferred or not-applicable items (`out_of_scope_only_sections`), because marking a note "not applicable" should not be a way to make a section look read; and long sections whose in-build quotes cover under a fifth of the text (`lightly_quoted_sections`), because a table of twenty requirements is otherwise "mapped" by one row. Quotes of deferred and not-applicable items do not count toward that share.
- **requirements**: totals by kind and disposition. `in_scope` is `via_needs` plus `bespoke`, and is the denominator for progress.
- **needs**: searched, decided, by verdict, by `decided_by`, by `coverage_outcome`, `reuse_pct`, `reference_count`, `registry_answered_pct`. Reuse means ADOPT, ADAPT or WRAP: the capability becomes part of the build. REFERENCE is reading something and then building, and is counted on its own.
- **progress**: in-scope requirements decided → locked → tasked → implemented → verified, each stage a subset of the one before, with `verified_cip_executed` and `verified_agent_reported_only`.
- **compliance**: lock usable or stale and why; stale tasks; **tasks worked on with no `check_against_lock` call on record**.
- **by_requirement**: per requirement, where it is in the chain, and the evidence behind each criterion (outcome, source, command, test reference, commit, dirty tree, repository). `verification_outcome` is what the evidence says on its own; `verified` is whether the requirement has reached the end of the chain.

The last compliance item is the available signal for whether the agent followed the workflow. Every lock check is an `audit_event`; one that was `blocked` for having nothing to check is not counted as a check of anything.

### Sections

The unmapped-section list is only as good as the section detection, and missing a heading is the expensive mistake: text under an unrecognised heading is folded into the section above, which then looks mapped. A PDR written with bold-line headings was one section; one quote mapped it and everything else vanished.

Recognised, outside fenced code blocks:

- ATX headings (`# Title`, indented up to three spaces, closing `#`s dropped);
- underlined headings: `===` and `---` (Setext), and the reStructuredText rows `~~~`, `^^^`, `***`, `+++`, which must run at least as long as the title;
- HTML (`<h2>Title</h2>`) and AsciiDoc (`== Title`) headings;
- a line that is only bold text;
- a short ALL-CAPS line, a short numbered title (`2.3 Billing`) or lettered one (`A. Compliance`), set off by a blank line;
- any other short line with no closing punctuation that stands between a blank line and a body (`Section 2: Security`).

A document pasted out of an email, every line prefixed `> `, is read without the prefix. When nothing at all reads as a heading, numbered titles with no blank line above them are taken as the structure.

Fences follow CommonMark: three or more backticks or tildes, closed by a fence of the same character, at least as long, with nothing after it. One departure: a bare row of tildes is not a fence. It is an underline in reStructuredText and a divider in plenty of hand-written documents, and read as a fence a pair of dividers hid every heading between them. Only a tilde fence that names a language is code. Headings are not looked for inside code, so a wrong answer about fences is expensive in one direction only: text taken for code hides its headings. Two rules keep the error on the cheap side. A fence that names a language while a block is open can only be an opener, so the earlier one was a stray and is dropped. And if a fence is still open at the end of the document, none of them is trusted and nothing is hidden. The cost is a few false headings from code comments.

A document with fewer than two sections is split on paragraphs, so there is always a denominator. The recognisers err towards finding too many sections: a false heading costs the human one glance.

Coverage is section-level. One quote marks its section as mapped; the lightly-quoted list is the mitigation, not a cure. Whether a mapped section holds a second requirement nobody extracted is for the person confirming to judge.

---

## 11. Sequencing

v1.0 said to wait for Step 14 (FastAPI + React) because the tools would call the core through that API. They do not. The MCP server calls `core` and `workers` directly and has done since Phase 12. This slice has no dependency on a FastAPI layer, and the React UI is not needed for approvals: the human gate is the CLI.

Steps 15–19 (judgment and consensus) are independent of this slice. When they land, the natural join is `llm_judgment`: a need CIP cannot decide from constraints is where a jury would add something.

---

## 12. What this does and does not enforce

The lock is governed cooperation. It binds because the coding workflow routes through CIP's tools. Nothing physically prevents an LLM with a shell from writing code outside them, or from importing `mcp_server.pdr_approvals` and calling `record_approval` itself, or from writing to the database.

What the design buys is that none of it can happen by drift, and that it leaves marks:

- there is no tool that approves, and the CLI refuses a non-interactive caller;
- an approval names an OS user and a channel, and is tied to a content hash and to the point in history it was made for;
- the human is shown the whole of what is hashed, wrapped and marked by CIP so the agent's text stays inside its own field;
- every gate refusal (`gate_refused`), refused task (`task_refused`), lock check, decision, approval request, lock, verification and overwritten evaluation (`evaluation_replaced`) is an `audit_event`. A refused call leaves its audit row and nothing else. Bad-input errors (`{"error"}`) are answers, not refusals, and are not audited;
- `coverage` reports tasks worked with no real lock check on record.

For hardening on a machine where an agent has a shell, add a deny rule for `cip-composer approvals approve` and `reject` to the agent's permission settings.

**Not built:** drift detection, comparing the target repository against the lock. Do not describe the lock as enforcement it is not.

**Still self-reported, or weaker than it looks:**

- `agent_reported` verifications.
- `cip-composer verify` can be run by anyone with a shell, the agent included, and it records whatever command it is given. It is evidence that a command exited 0 at a clean commit. Whether the command tests the criterion is for a person to judge from the command, which `coverage` returns.
- The free-text `prohibited_changes` on a task: returned as a reminder, never evaluated.
- Whether a requirement is atomic, and whether a section the agent left unmapped, or mapped with one quote, really holds nothing else. The human is shown the lists; CIP cannot read the section for them.
- The sweep has a ceiling of 100 hits per term. Past it CIP says so and declines to conclude that nothing fits; it does not look further.
- The known-provider list and the "any non-zero digit" cost rule are floors. A provider not on the list, or a cost written in words, is taken as declared. Both are still printed in the summary. A provider name outside ASCII is flagged as unchecked; nothing identifies it.
- **Search terms are ASCII.** That closes look-alike terms completely and costs something real: a need cannot be swept with a term in another alphabet, so a registry entry that could only be found by a non-English word in its description will not be found by the mandatory sweep. Names and keys in the registry are ASCII. A custom query (`search_query`) is not restricted, and is extra evidence, never the sweep.
- A license allowlist entry is checked for the *form* of an SPDX identifier, not against a list of them. `Apache2` or `MIT-License` has the form, matches no license, and gives every candidate a confirmed gap. The allowlist is in the summary the human confirms, as written.
- A 0 and an O, or a 1 and an l, are different ASCII characters that some fonts draw alike. Terms are shown lower-case and in quotes; that is all CIP does about it.
- A fail is kept through a rewrite of its requirement, but not through a change to its own criterion's text: the criterion row goes, and its evidence with it. The audit trail records what was dropped; coverage does not show it.
- When `scaffold_pipeline` fails halfway there is no list of what it wrote, so every file in its target folder that it could have written is logged as written by it, including one that was already there. The label errs towards saying a tool wrote a file a person did.
- The document reader accepts any `.md`, `.txt`, `.rst`, `.markdown` or `.text` file the server can read unless `CIP_DOCUMENT_ROOTS` is set.
- Heading detection is heuristic. It is tuned to over-detect, and it will still meet a layout it does not read.
- The wrap mark depends on CIP knowing the terminal's width. A terminal narrower than 27 columns, a font that draws a character wider than two columns, or a viewer that re-flows text (a pager on a narrower screen than the terminal it was started in, a web log viewer) can still break a row where CIP did not.
- The registry fingerprint counts rows in the large tables and does not read them. Evidence is append-only, so that is sound today; an in-place edit to an evidence row, a scorecard or a source binding would not be noticed. Capability rows, version states and evaluations are read.
- The fingerprint covers the whole registry, not the part a need's terms match. While an ingest or analysis batch is running, a decision will be refused until it finishes and the need is swept again. That is the safe direction, and it is a nuisance.
- `cip-composer verify` records which repository it ran in and shows it. It does not know which repository is the project's, so a pass in some other repository counts until a person reads the line.
- Low-confidence and unidentified licenses are treated as not read, by the matcher as well as in what is reported. A wrong identification at high confidence is taken as read.
- Unlinked evidence is attributed to a version the way promotion attributes it: everything extracted from the pinned revision. A repository holding several components is not split.
- Text is checked for invisible and look-alike characters, not for meaning. Right-to-left letters may display in a different order from the one they are stored in, and a pile of combining marks on one letter can overdraw the rows above and below it in some terminals.
- `--json` output is not wrapped. Read approvals in the default view.
- Older tools on the same MCP server are listed in §6 with what holds each one. A tool added later that writes requirements, files or registry rows would need the same treatment.

**Not verified on Windows** (reasoned from the code and CPython's documented behaviour; there was no Windows machine in the test environment):

- `cip-composer verify` on a `.cmd` or `.bat` shim (`npm`, `pnpm`): Windows runs those through `cmd.exe`, which re-parses the arguments. "Without a shell" does not strictly hold for them.
- A command whose grandchildren keep the output pipes open may run past the timeout before `verify` returns.

**A limit of the registry, not of this slice:** CIP computes a verdict only for candidates it holds the right evidence for. Until more of the registry has been through Step 6 analysis, most reuse decisions will be `llm_judgment` with `unanalysed_candidate`, and `coverage` will show that in `needs.by_decided_by`. That is the accurate picture, and it is the number to watch as analysis coverage grows.

---

## 13. Acceptance criteria

Each is covered by tests in `tests/test_pdr_workflow.py`, `tests/test_pdr_core.py`, `tests/test_pdr_core_hardening.py`, `tests/test_pdr_review_regressions.py`, `tests/test_pdr_review2_regressions.py`, `tests/test_pdr_review3_regressions.py`, `tests/test_pdr_review4_regressions.py`, `tests/test_pdr_cli.py` and `tests/test_pdr_skill_contract.py`.

1. A PDR produces requirements whose quotes CIP located itself. A quote not in the PDR writes no row.
2. Search and decision are refused until a human approves the current scope, including its search terms. Any edit after approval re-closes the gate, and so does a return to content approved earlier.
3. A search that finds nothing is logged, and BUILD is then allowed and labelled a cold-start build.
4. No decision exists without the mandatory sweep for the need as currently defined, against the registry as it currently is. The sweep cannot be narrowed by a filter, a limit or project policy, and it says when it hit its ceiling.
5. Every decision records who decided. An override needs a rationale, and reaches the human with what CIP computed and the agent's reason. CIP claims a reuse verdict only with the evidence for the candidate it chose, and a BUILD only with the evidence for every candidate, a sweep that saw them all, and rules that accept none of them. Evidence of every kind is for the pinned revision, a license counts only if CIP identified it, and what CIP counts as held is what its matcher reads.
6. A refused re-decision does not revoke an existing build authorisation.
7. No lock exists without an approval for its exact payload, made against the lock it replaces, the request it follows and the confirmation of the scope it was requested under. Only the most recent lock request can be written, and "most recent" is the order of writing. The approver and the note are not tool arguments. An approval writes one lock.
8. A lock row cannot be edited. A changed lock is a new version with a diff, and the diff never reads "none" for content that changed.
9. No build task exists without an active, usable lock, and a BUILD need passes Step 13. A lock is usable only while the scope it was approved for is the confirmed scope.
10. `check_against_lock` fails closed, takes structured input, checks capabilities, providers, dependencies, project decisions and paths, escalates what it does not understand, and logs every call.
11. `verified` requires a passing latest verification for every acceptance criterion, recorded by a task that is still live, on work reported implemented under the locked decision; it stops being true when any of that does. A fail is never erased by what happens to the task that recorded it, by rewording its requirement, or by a later `not_run`.
12. `coverage` reports unmapped, out-of-scope-only and lightly quoted PDR sections and divides progress by in-scope requirements. A dirty-tree pass is not CIP-executed, whether the tree was dirty before the command or made dirty by it.
13. A fresh session can resume from `pdr_status` alone, including across a lock approval.
14. The skill and the server name the same tools, and no tool takes an approver or an approval note.
15. Import-linter contracts unchanged.
16. Text supplied by the agent cannot change what the human's terminal displays outside its own field: no control sequence reaches the terminal, and every line a decision command prints is wrapped by CIP and marked where it continues.
17. The human can tell which PDR they are confirming and which architecture document they are locking, and whether each was read from a file.
18. Gate refusals are in the audit trail.
19. Bad input returns an error; no tool raises on it.
20. Existing v1.0 rows can still be deleted, never count as verification, never crash a tool, and are not adopted by a later lock.
21. A second confirmation shows what changed since the first, and what was retired.
22. A quote is at most 600 characters and stays inside one section of the current PDR, however the row got there; where it sits is part of the confirmed scope.
23. Work that rests on a need whose decision changed since the lock is on hold until a relock.
24. There is one live lock request per project.
25. What the human confirms as search terms is, character for character, what the registry is asked, and every character is ASCII.
26. A decision is refused if anything a search reads has changed since its sweep, in the registry or in the evaluation store.
27. `complete` means every in-scope requirement has reached the end of the chain in §9.
28. Each line of a lock describes the revision that decision pinned, also when two needs pin different revisions of one capability.
29. A lock ends the tasks it does not carry; none returns under a later lock.
30. The terminal records an approval only for a request that is still live when the code has been typed.

---

## 14. What v1.0 got wrong, and where it was closed

| # | Gap | Closed by |
|---|---|---|
| 1 | A PDR requirement is not a capability need | §2: `kind`, dispositions, `requirement_need_link`, `record_capability_needs` |
| 2 | Cold start: empty registry blocked BUILD forever; banked evaluations invisible to the PDR flow | §3: `requirement_search`, mandatory sweep, `coverage_outcome`, evaluation-store recall |
| 3 | The LLM supplied the verdict | §3: CIP computes where it has grounds to; `decided_by`; overrides and judgments listed at lock |
| 4 | Gates had no state; the LLM named its own approver | §4: `approval_request` / `approval`, content-bound, history-bound, CLI-only |
| 5 | Step 13's gate was off the PDR path | §7, §8: lock creates `build_authorization`; task gate calls `can_generate_for` |
| 6 | Coverage measured against extracted requirements; offsets supplied by the LLM; the PDR text was not stored | §1, §10: stored text, CIP-located quotes, `unmapped_sections` |
| 7 | `verified` was self-reported with no evidence | §9: criteria rows, structured evidence, latest wins, `cip-composer verify` |
| 8 | `check_against_lock` was free-text, failed open, never read `permitted_paths`, logged nothing | `core.pdr.lock_check`, audit rows |
| 9 | The lock held decisions only; the hash was not canonical; JSONB references could be orphaned | §7: full payload, canonical hash, `decision_record` references, immutability trigger |
| 10 | No way for a fresh session to resume | `pdr_status` |
| 11 | No change flow, no rule for a revised PDR | §7: relock diff, staleness, source supersession, task currency |
| 12 | The coding skill was named as what makes the lock bind, and never built | `cip-pdr-build` skill, contract test, compliance signal |
| 13 | Step 14 dependency overstated | §11 |
| 14 | Playbook and spec did not connect: three verdict sets, no evidence depth, recall asserted but not wired | `evaluation.evidence_depth`, `Wrap`, recall in `search_for_requirement`, playbook v2 |

---

## 15. Existing data

Migration 030 was applied on top of a database holding v1.0-shaped rows and checked: criteria backfilled, legacy verification and evaluation rows kept and marked `legacy`, a v1.0 task with a pass row deletes cleanly, project cascade delete still works.

A project created by the v1.0 tools degrades in the closed direction:

- its requirements have no disposition, so confirmation is blocked until they are dispositioned;
- its PDR source has no stored text; re-run `ingest_pdr` with the same file and the text is filled in against the same hash;
- its lock has no approval record, so it is treated as stale: relock to put it under a recorded approval;
- its tasks can be abandoned and nothing else, before a relock and after one;
- its verification rows are `legacy` and count for nothing;
- a `source_span` in a shape v1.1 does not write (a string, a list, a quote that is a number) reads as "no quote", which blocks confirmation until the requirement is re-recorded. It used to raise in every tool for that project.

Evaluations saved before 030 have `evidence_depth` NULL, shown as `unrecorded`. `scripts/seed_evaluations.py` re-seeds them without inventing a depth.

---

## 16. Independent review: how v1.1's first cut was beaten, and what changed

After the gap closure passed its own tests and bug-injection run, the code went to a reviewer with no part in writing it and one instruction: get round it. Twenty findings, each reproduced with a script. Every one has a regression test in `tests/test_pdr_review_regressions.py` or `tests/test_pdr_core_hardening.py`, and a bug-injection mutant that re-introduces it.

| # | How it was beaten | What changed |
|---|---|---|
| 1 | The mandatory sweep could be narrowed to nothing: `ecosystem=""` filtered on the empty string and still counted as unfiltered; `limit=1` hid the candidate that fit; search terms could be swapped after confirmation | Empty filters are no filters; the sweep takes at least 20 hits per term; search terms and problem types are confirmed content (§2, §3) |
| 2 | Approvals could be replayed: an old lock approval matched by hash and wrote a new lock nobody approved; a request could be approved after the lock it diffed against was replaced; reverting to earlier scope re-opened gate 1 on its old approval | Approvals are bound to history: `follows_approval_id`, `replaces_lock_version`, obsolete requests, one lock per approval (§4, §7) |
| 3 | Escape sequences and newlines in agent-written text erased warning lines and forged sections in the summary the human reads | Every field rendered as one printable line; second guard in the CLI (§4) |
| 4 | `verified` outlived its evidence four ways, and a pass could be recorded against a criterion rewritten while the lock was stale | Demotion on every path, effective status derived on read, nothing recorded against a stale lock (§9) |
| 5 | Two verifications in one call shared a timestamp; which was "latest" was arbitrary | Identity-column ordering (§5, §9) |
| 6 | `check_against_lock` passed what it did not understand: `action: "use"` on an unapproved provider, non-string paths, unknown keys; project decisions were stored and never checked; a NUL raised before the audit row | Unknown is escalated, project decisions are checked, unreadable input is `blocked` and logged (§6) |
| 7 | The reuse pin was skipped by linking only the requirement; `permitted_paths: ["**"]` permitted the repository | Pin resolved through requirements and filled by CIP; leading wildcards refused (§8) |
| 8 | `cip_executed` could be minted with a no-op command, and on a dirty tree | Dirty tree does not count; the command is returned for a person to read; the limit is stated (§9, §12) |
| 9 | Section detection missed bold, Setext and numbered headings, and an unclosed fence swallowed the document, so unmapped sections disappeared | Wider heading recognition, fence handling, paragraph fallback (§10) |
| 10 | The PDR could be swapped through `pdr_text` and the summary did not identify the document | File path and SHA-256 recorded and shown; inline text and a replaced PDR called out; file re-checked at approval (§4) |
| 11 | "Analysed" meant any evidence at all, so a license file answered for an interface constraint | Evidence judged per constraint kind (§3) |
| 12 | A derived slug silently overwrote a different requirement | Refused unless the slug is explicit (§2) |
| 13 | `pdr_status` could not resume across the lock approval: the second call needed inputs a new session did not have | `lock_architecture(approval_request_id)`; status returns the approved request and what the lock froze (§6, §7) |
| 14 | The documents overclaimed: refusals "logged" that were not, a summary called complete that omitted cost commitments | Refusals audited; the summary renders the whole payload; this document checked claim by claim (§12) |
| 15 | `approval_note` was supplied by the agent | Removed; the note is the approver's (§4) |
| 16 | Windows desktop: test output that would not decode, exit codes over 32 bits, cp1252 consoles, relative paths resolving to CIP's folder, UTF-16 JSON, a spoofable approver name, `.cmd` shims, end-of-input at the prompt | §4, §6, §9; `tests/test_pdr_review_regressions.py::TestWindowsDesktop` |
| 17 | Re-ingesting an earlier PDR was a dead end | `activated_at`; an earlier text can be made current again (§5, §7) |
| 18 | Migration 030 broke deleting a v1.0 task that had a pass row | `legacy` flag (§5) |
| 19 | Tracebacks on NUL, surrogates, NaN and wrong types; `commit_sha: "unknown"` accepted; a string stored as a list of characters | Input hygiene at the door (§6) |
| 20 | `approvals approve %` matched any request; the document reader read any file; an in-scope constraint needed no criteria | Hex-prefix lookup; suffix allowlist and `CIP_DOCUMENT_ROOTS`; criteria required in scope (§2, §4, §6) |

---

## 17. Second independent review: how the fixed code was beaten, and what changed

The fixes in section 16 went to a second reviewer, again with no part in the writing and the same instruction. About 150 attempts, each a script. One critical finding, eight high, ten medium and a list of smaller ones. Every one has a regression test in `tests/test_pdr_review2_regressions.py` or `tests/test_pdr_core_hardening.py`, and a bug-injection mutant that re-introduces it. The reviewer's own scripts were re-run against the fixed code and no longer reproduce.

| # | How it was beaten | What changed |
|---|---|---|
| C1 | Confirm scope A and lock it; add a requirement, confirm B; retire the addition. The hash is A's again, so the lock read as current and tasks, lock checks and verification went ahead on a scope the human's latest confirmation had replaced | A lock is stale unless the current scope is the confirmed scope (§7) |
| H1 | A BUILD the agent wanted was recorded as CIP's own decision: one analysed candidate that failed ranked above unanalysed ones that might fit, or a candidate with no scorecard, and the rules "rejected them all" | CIP claims BUILD only with evidence for every candidate; a missing scorecard is missing evidence (§3) |
| H2 | The sweep still stopped at 20 hits per term, silently | Ceiling of 100, `truncated` recorded, no "nothing fits" from a truncated sweep, flag at lock (§3) |
| H3 | One requirement quoting the whole PDR mapped every section; a `not_applicable` fragment did the same; the summary showed 200 characters of the quote | Quote ceiling, no crossing a heading, out-of-scope-only and lightly-quoted lists, full quotes shown (§2, §4, §10) |
| H4 | Adding an uncheckable constraint to a need turned a real GPL finding into "unanalysed" | The two flags are independent; gaps are split into those that rest on evidence and those that record its absence (§3) |
| H5 | A second confirmation looked like the first: no diff, and retired requirements simply vanished | Changes since the last confirmation, first; retired items listed (§4) |
| H6 | `verified` and `complete` were reported on evidence from abandoned or stale tasks, and on passes recorded against a requirement since rewritten | Evidence counts only from live tasks; a rewritten requirement's criteria are replaced (§2, §9) |
| H7 | The relock "Changes" section said "none" when a license, a provider's data handling and a cost had changed | The diff is complete by construction, with a catch-all (§7) |
| H8 | A field padded to the terminal's width pushed forged lines onto rows of their own; `pdr-status` printed a project name's escape sequences raw | CIP wraps and marks continuation rows; one output filter for the whole CLI; names refused at ingest (§4) |
| M1 | `check_against_lock` allowed installing a REFERENCE capability, and read a list where a name belonged as "nothing given"; a `blocked` check counted as the task's lock check | REFERENCE is not a dependency; wrong types escalate; only real checks count (§6, §10) |
| M2 | "The registry had nothing" and the banked-match flag could be made wrong: problem types added after the sweep, a banked evaluation overwritten, unevaluable hits reported as no coverage, a sweep older than the registry, project policy hiding hits | Tags in the fingerprint, `evaluation_replaced`, `found_but_unevaluable`, registry freshness check, sweep unfiltered by policy (§3) |
| M3 | A task made under a v1.0 lock came back to life after a relock | Never adopted (§8, §15) |
| M4 | A v1.0 `source_span` that was not an object made every tool raise for that project | Read as "no quote" (§15) |
| M5 | Section detection still lost sections: a stray fence, indented and HTML headings, rst underlines, plain title lines | §10 |
| M6 | Tracebacks: UUIDs in other spellings, `limit=inf`, two sessions racing for one key | Canonical ids, bounded limit, a retry answer (§6) |
| M7 | A 16,000-character line of `#` took seconds per call; every quote re-collapsed the whole PDR | Linear heading patterns, a line-length bound, one `QuoteLocator` per document |
| M8 | Evidence could come from a different revision than the one pinned | Pinned revision only, in the fit matcher and every display (§3) |
| M9 | Work continued on a need whose decision had changed since the lock | On hold: progress, verification and lock check (§7) |
| M10 | The architecture document was a bare hash at approval | Path and file SHA-256 in the payload and the summary (§4, §7) |

Smaller findings, all closed: flags that trusted the agent's own labels (§7); CLI commands that exited 0 on an all-invalid batch or a blocked request (they exit 2); `pdr-lock --inputs` silently dropping a misspelt key (refused); `verify` not re-checking the tree after the run, not recording which repository it ran in, and raising on `--criterion ²` (§9); obsolete requests listed as waiting (§4); derived and over-long slugs overwriting rows (§2); invisible-only values and two spellings of one name accepted (§2); look-alike search terms (§2); unbounded inputs (§6); two `record_decision` refusals and the Step 13 refusal missing from the audit trail (§12); the non-atomic decision write (§3); spans outside the confirmed hash (§2); a blank `CIP_DOCUMENT_ROOTS` meaning no confinement (§6); `quoted_char_pct` over 100, a slug listed twice on a task, REFERENCE counted as reuse (§10).

The reviewer also listed 22 places where this document or the skill claimed more than the code did. Each claim was either made true or rewritten; that is why several sections above now state a limit where they used to state a guarantee.

**What the second review could not break**, for the record: writing a lock without a matching approval, or replaying one, including under a four-way race; the canonical hash across a JSONB round trip; atomicity of refusals; the task gate's path rules; the document reader's confinement; migration 030 on a v1.0 database.

---

## 18. Third independent review

The fixes in section 17 went to a third reviewer with the same brief. About 190 distinct attempts, plus fuzzing (14,400 calls across every tool and nested field), a concurrency run (2,079 calls on 8 threads), 193 mutations of the two diffs and 4,000 wrap trials.

**No critical finding.** It could not write a lock, open gate 1, or record an approval without a human approval for that exact content. It found nine high findings (CIP or the human made to assert something false), twelve medium and nine low. Every one has a regression test in `tests/test_pdr_review3_regressions.py` or `tests/test_pdr_core_hardening.py` and a bug-injection mutant; the reviewer's scripts were re-run against the fixed code.

| # | How it was beaten | What changed |
|---|---|---|
| H1 | CIP recorded BUILD as its own decision while its rules would have given a lower-ranked candidate ADAPT. The rules ran on rank 1 only, and "rejected every candidate" was written about one | Rules run over every candidate; BUILD is CIP's only if none is accepted; `build_over_acceptable_candidate` (§3) |
| H2 | A LICENSE file the extractor could not identify counted as a license CIP read and rejected | Only an identified license is evidence (§3) |
| H3 | One search term holding a comma-separated list displayed like the list and matched nothing; a zero-width selector inside a term or a provider name did the same | Terms are plain and shown quoted; names are folded for matching; invisible and look-alike characters refused (§2, §7) |
| H4 | "Evidence for the pinned revision" held for the license only; interfaces and dependencies read on an older revision answered for a newer, unread one | All three kinds scoped to the pinned revision; one loader (§3) |
| H5 | The lock showed "pinned: R1" beside a license read from R2, bound after the decision; a GPL flag vanished | The lock reads each decision's license from the revision that decision pinned (§3) |
| H6 | An approved lock request stayed writable after the human approved its replacement: the second review's critical finding, moved to gate 2 | Only the latest request is written; request history is in the hash (§4, §7) |
| H7 | Abandoning a task erased a failure CIP had itself observed, and the older pass underneath came back | A fail stands whatever happens to its task (§9) |
| H8 | The legacy `import_requirements` tool rewrote a verified requirement and kept its pass, took unbounded text, and put unhashed constraints on PDR rows | Refused for a project with a PDR (§2, §6) |
| H9 | A quote recorded against a flattened copy of the PDR was carried onto the real one and mapped five sections | Carry-forward applies the quote rules; every stored anchor is checked on read (§2) |
| M1 | The architecture document was not re-checked at approval | Checked, as the PDR is (§4) |
| M2 | `scaffold_pipeline` wrote a file with the agent's text, which was then ingested as "a file" | Tool-written files are logged and labelled (§4) |
| M3 | `verified` was granted on a pass from an abandoned task while every read said otherwise | One evidence rule for the write and the reads (§9) |
| M4 | The lock check allowed a drifted need's capability or dependency unless a path came with it; `pdr_status` said `complete` after a locked decision was changed, and with every task still planned | Drift checked for all three; verified is the end of a chain; no pass against a planned task (§7, §9) |
| M5 | An approved request that could no longer be written was still offered; a pending lock request survived requirement edits | A request is live only while replaying it gives what it describes (§4) |
| M6 | A judged BUILD over a truncated, empty sweep was approved by the human and then refused by Step 13, for good | The proof-of-search marker is kept (§7) |
| M7 | The banked-rewrite flag required `banked`; the search does not. An overwrite before the first sweep, or before the project existed, escaped it | The search's own test; a 30-day window (§3) |
| M8 | Ingesting or analysing an existing match changed no count, so the decision rested on the old sweep | A fingerprint of what the search reads (§3) |
| M9 | A not-applicable note quoting four rows emptied the lightly-quoted list | In-build quotes only (§10) |
| M10 | A padded project name forged rows outside the wrapped summary | Every line wrapped; names capped (§4) |
| M11 | A migrated v1.0 quote and its offsets were trusted once the text was filled in | Anchors checked against the text on read (§2) |
| M12 | A lock row whose `decisions` was not a list crashed every tool for the project | Read as an unreadable lock, which is stale (§7) |

Smaller findings, all closed: fields made only of variation selectors or combining marks (§2); a diff line that read "X -> X" (§7); the unevaluable flag describing a policy exclusion as a missing source; the repository of a CIP-executed run stored and shown nowhere (§9); tilde dividers read as code fences, email-quoted documents and numbered titles with no blank line (§10); a task title with line breaks (§8); two terminals approving one request raising instead of answering; a need with no problem types never getting the banked flag, unsaid (§3); a refused `verified` logged as a gate refusal (§8). And the override that reached the human as a flag, with neither CIP's verdict nor the agent's reason (§4).

Twenty-one places where this document claimed more than the code did were listed. Each was made true or rewritten.

Two things found while fixing, not by the reviewer, and worth recording because of how they were found. The rule "evidence is for the pinned revision" was applied to one of two copies of the candidate loader; a test written to kill a mutant caught it, and there is now one loader. And an edit to the approval writer left the approval's audit event unreachable; the existing end-to-end test caught it on the next run.

**What held**, for the record: the second review's critical finding and three cousins of it; replay of lock approvals across versions; one lock per approval; the task and verification gates (12 path patterns, the reuse pin, Step 13, every pass rule, latest-wins, stale-lock holds); all 80 gate-sequence checks; fit freshness at decision time; 14,400 fuzz calls with no exception; the concurrency run; `wrap_block` never producing an over-wide or unmarked row; migration 030 on a v1.0 database.

**What three rounds show, and do not.** The findings went 20 (several of them ways to get a lock or an approval nobody gave), then 19 with one such, then 30 with none. The count did not fall; the severity did. What changed is where the holes are. The first round got round the gates. The second got round one gate by a sequence. The third could not get a lock or an approval it was not given, and instead found CIP saying things it had not checked: about candidates it did not rank first, licenses it could not read, revisions it had not opened, terms that were not what they looked like. That is the surface that remains: not "can the agent act without approval" but "is everything CIP tells the human true". It is larger than the gate logic, it grows with every field that is displayed, and a fourth reviewer would find more of it. Read the summaries as a careful clerk's work and not as proof. Section 12 lists what is known to be weak.

---

## 19. Fourth independent review

The third round's fix pass was large, and large fix passes are where new holes come from. So the fourth review was narrow: had that pass broken a gate, or left one of its own fixes half done? About 135 distinct attempts, and a 250-step random walk over the lock state machine that checked every gate invariant after each step and found no violation.

**No critical finding.** It could not write a lock from an approval that was not the human's latest word, write one without an approval for its exact payload, get two locks from one approval, or leave an approved request unwritable for good. It found four high findings, eight medium and eight low. Every one has a regression test in `tests/test_pdr_review4_regressions.py` or `tests/test_pdr_core_hardening.py` and a bug-injection mutant, and the reviewer's scripts were re-run against the fixed code.

| # | How it was beaten | What changed |
|---|---|---|
| H1 | A component ingested from registry metadata and analysed afterwards had its evidence unlinked (promotion is a no-op once a binding exists). CIP counted the succeeded stages as "evidence held", the matcher read the empty link tables, and CIP adopted a component whose manifest named the forbidden dependency | The matcher reads the evidence on the pinned revision, linked or not; "held" is defined by the same rows (§3) |
| H2 | "Only an identified license is evidence" was applied to what CIP reported and not to what the matcher read: a low-confidence BSD mention satisfied an allowlist beside an identified GPL | One definition, used by the loader (§3) |
| H3 | Two needs that chose one capability version at different revisions shared one facts entry; one lock line printed the other revision's license, and a GPL flag could vanish | Facts are looked up per decision, by revision and asset (§3) |
| H4 | A search term or provider name written entirely in look-alike letters passed the rule against mixing alphabets | Terms are ASCII; a provider name outside ASCII is flagged as unchecked (§2, §7) |
| M1 | A lock approval given under one confirmation of scope A was written after the human had confirmed B and then A again | The payload names the confirmation (§4, §7) |
| M2 | "The latest lock request" was ordered by transaction start: a call that began first and finished last produced a request that was reported superseded and could not be revived | Ordered by an identity column; one lock call per project at a time; the write looks again (§4) |
| M3 | The evaluation store was not in the registry fingerprint: an evaluation banked after the sweep left a BUILD recorded as a cold start | It is, with version states (§3) |
| M4 | `scaffold_pipeline` raising after it had written README.md left a file with no log entry, ingested as a person's document | Logged in a `finally` (§6, §12) |
| M5 | Rewording a requirement and reverting it erased a failure CIP itself had observed, with no human step | A rewrite keeps the fails; dropped evidence is audited (§2, §9) |
| M6 | A criterion holding a line break put a row of the agent's own on the `verify` screen; the search and lock-check screens printed stored values the same way | Stored text is one line; those screens print one line per value (§2, §4) |
| M7 | Migration 030 was edited in place during the work | Stated, with the condition that makes it legitimate (§5) |
| M8 | An allowlist entry that is not an SPDX identifier matched nothing, and the BUILD was CIP's own with no flag | Entries are validated and trimmed (§2) |

Smaller findings, all closed: a stale task that came back to life two locks later (§8); a pass that still counted with its task back at `planned`, and a `not_run` that hid a fail (§9); a rationale made of a zero-width space accepted as a rationale (§3); a term with two spaces (§2); a passage in the PDR longer than the ceiling, found by a short quote with the whitespace collapsed; a blocked anchor still counted as mapping its section (§10); and the approval terminal recording a request that had gone dead while it was on screen (§4).

Six places where this document claimed more than the code did were listed, among them "evidence of every kind is for the pinned revision", "what is confirmed is what is asked" and "nothing that is hashed is left unshown" (the request a lock follows was hashed and not printed). Each was made true.

One thing found while fixing, not by the reviewer. The first version of the per-project lock was taken before the argument checks and re-taken after the request's commit, so a call that returned an input error, or simply returned "awaiting approval", went on holding the project until its connection closed. Re-running the reviewer's own race script against the fix is what showed it: the second caller hung. The lock is now taken after the argument checks, and every path below it ends the transaction.

Bug injection for this round: 327 code mutants (the earlier rounds' 272, with fifteen re-anchored where this pass moved the code, and 55 new ones, one or more per finding) and 17 schema mutants. All are caught. One was not on the first run, and the reason is worth a line: the old mutant "a quote has no length ceiling" survived because the ceiling is now enforced twice, on the quote as sent and on the passage as it stands in the PDR, and removing the first check alone changes nothing a caller can see. It was redefined to remove both, and a separate mutant removes the second alone. Six of the new tests exist because a mutant would otherwise have had nothing to fail: the asset half of a pin, the request re-made the latest, the look before the lock write, an evaluation rewritten in place, the line naming the request a lock follows, and the `verify` screen.

**What held**: every gate-sequence attack on the request history (approve A, request B, replay A; approve, relock, replay; rejected requests; requests against an older lock; requests in another project; same-timestamp ties); one lock per approval under concurrency; the second review's critical finding and its cousins; the evidence rules across abandon, relock, retire and defer; the anchor checks; the wrap, the prompt and the empty-output cases of the CLI stream; the legacy tools.

**What four rounds show, and do not.** The severity has stayed where the third round left it: no gate opened, and what was found was again CIP asserting more than it had checked, this time mostly in the seams of the previous fix (a rule applied where a thing is reported and not where it is computed; a lookup keyed one level too coarse; a rule against mixing that said nothing about not mixing). Three of the four high findings were exactly that. A fix pass adds such seams as fast as a review removes them, and **the fixes described in this section have not been reviewed by anyone but their author.** They are tested, each test is known to fail when its fix is taken out, and the reviewer's scripts no longer reproduce. That is weaker than a review, and it is where this work stops: a fifth round would find something, and so would a sixth. What the four rounds do support is narrower and worth having: the two gates have now been attacked directly four times, by sequence, replay, concurrency and random walk, and since the second round nothing has got a lock or an approval the human did not give. What they do not support is reading a summary as proof. Read it as a careful clerk's work, check the lines that matter to you against the source, and use section 12.
