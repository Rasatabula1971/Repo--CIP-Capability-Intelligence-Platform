"""
check_against_lock — a deterministic test of a proposed change.

v1.0 took a free-text description, string-matched a provider name
against requirement slugs, never read permitted_paths, and answered
"allowed" when there was no lock at all. This version:

  - takes STRUCTURED input and refuses to judge a change it was only
    given prose for
  - fails closed: no lock, a stale lock, an unknown need or an unknown
    task all stop the change
  - cites the specific locked item each reason rests on

Verdicts:
  allowed         nothing in the change touches a locked item
  needs_approval  the change alters a locked decision or goes outside
                  the task's permitted paths — re-decide and relock
  blocked         the question cannot be answered: no usable lock, or
                  the input says nothing checkable

Fail-closed also means: anything not understood is not waved through.
An unrecognised key, a malformed entry or a non-string path escalates,
because a part of the change that was not checked is not "allowed".

What this cannot do: `prohibited_changes` on a task is free text. It is
returned as a reminder, never evaluated.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any

from core.pdr.text import one_line

# Verdicts that make a capability a dependency of the build. Mirrors
# core.pdr.decisions.DEPENDENCY_VERDICTS (not imported: this module is
# kept free of the decision rules). A REFERENCE decision locks the right
# to read something, not to install it.
_DEPENDENCY_VERDICTS = ("ADOPT", "ADAPT", "WRAP")
# A proposed change is a few paths and names. Anything much larger is not
# a change description, and it would be stored whole in the audit trail.
MAX_CHANGE_CHARS = 20_000

ALLOWED = "allowed"
NEEDS_APPROVAL = "needs_approval"
BLOCKED = "blocked"

_STRUCTURED_KEYS = (
    "changed_paths", "capability_changes", "provider_changes", "dependency_adds",
    "project_decision_changes",
)
_TOP_LEVEL_KEYS = frozenset((*_STRUCTURED_KEYS, "description", "build_task_id"))
_ITEM_KEYS = {
    "capability_changes": frozenset(("need", "capability_version_id", "verdict")),
    "provider_changes": frozenset(("name", "action")),
    "dependency_adds": frozenset(("ecosystem", "name")),
    "project_decision_changes": frozenset(("key", "choice")),
}


@dataclass(frozen=True)
class LockView:
    version: int
    stale_reasons: tuple[str, ...]
    # need slug -> {need_id, verdict, capability_version_id, capability_key, pinned_revision}
    decisions: dict[str, dict[str, Any]]
    approved_providers: tuple[dict[str, Any], ...]
    project_decisions: tuple[dict[str, Any], ...] = ()
    # Needs whose decision has changed since this lock froze it. What the
    # lock says about them is no longer what was decided.
    drifted_needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskView:
    id: str
    title: str
    status: str
    is_current: bool
    permitted_paths: tuple[str, ...]
    prohibited_changes: tuple[str, ...]
    # Needs this task rests on whose decision has changed since the lock.
    drifted_needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class LockCheckResult:
    verdict: str
    reasons: tuple[str, ...]
    cited: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reminders: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def evaluate_change(
    *,
    lock: LockView | None,
    task: TaskView | None,
    task_requested: bool,
    change: Any,
) -> LockCheckResult:
    if lock is None:
        return LockCheckResult(BLOCKED, (
            "No active architecture lock. Nothing may be built or changed until "
            "decisions are locked (lock_architecture).",
        ))
    if not isinstance(change, dict):
        return LockCheckResult(BLOCKED, ("proposed_change must be an object.",))
    try:
        size = len(json.dumps(change, default=str))
    except (TypeError, ValueError):
        return LockCheckResult(BLOCKED, ("proposed_change could not be read.",))
    if size > MAX_CHANGE_CHARS:
        return LockCheckResult(BLOCKED, (
            f"proposed_change is larger than {MAX_CHANGE_CHARS} characters. Describe "
            f"one change: the paths, capabilities, providers or dependencies it touches.",
        ))

    if not any(change.get(k) for k in _STRUCTURED_KEYS):
        return LockCheckResult(BLOCKED, (
            "The change has no checkable content. Supply at least one of "
            "changed_paths (with build_task_id), capability_changes, "
            "provider_changes, dependency_adds or project_decision_changes. "
            "A description alone cannot be tested against the lock.",
        ))

    reasons: list[str] = []
    cited: list[dict[str, Any]] = []
    verdict = ALLOWED

    def escalate(reason: str, cite: dict[str, Any] | None = None) -> None:
        nonlocal verdict
        verdict = NEEDS_APPROVAL
        reasons.append(reason)
        if cite is not None:
            cited.append(cite)

    # Anything this function does not understand is something it did not
    # check. Unknown keys escalate rather than pass.
    for key in sorted((k for k in change if k not in _TOP_LEVEL_KEYS), key=str):
        escalate(
            f"Unrecognised key '{_echo(key)}'. It was not checked; use "
            f"{', '.join(_STRUCTURED_KEYS)}."
        )

    def items(key: str) -> list[dict[str, Any]]:
        """The well-formed entries under `key`; malformed ones escalate."""
        out = []
        for i, entry in enumerate(_list(change.get(key))):
            if not isinstance(entry, dict):
                escalate(f"{key}[{i}] must be an object.")
                continue
            unknown = sorted(_echo(k) for k in entry if k not in _ITEM_KEYS[key])
            if unknown:
                escalate(
                    f"{key}[{i}] has unrecognised key(s) {unknown}; allowed: "
                    f"{sorted(_ITEM_KEYS[key])}. The entry was not checked."
                )
                continue
            # A list or a number where a name belongs used to read as
            # "nothing given", and "nothing given" as "no change".
            wrong = sorted(k for k, v in entry.items()
                           if v is not None and not isinstance(v, str))
            if wrong:
                escalate(
                    f"{key}[{i}]: {', '.join(wrong)} must be text (or null). "
                    f"The entry was not checked."
                )
                continue
            out.append((i, entry))
        return out

    for key in ("description", "build_task_id"):
        if change.get(key) is not None and not isinstance(change.get(key), str):
            escalate(f"'{key}' must be text.")

    for r in lock.stale_reasons:
        escalate(f"Lock v{lock.version} is stale: {r}")

    # --- capability changes ---------------------------------------------
    for i, cc in items("capability_changes"):
        need_ref = _s(cc.get("need"))
        if not need_ref:
            escalate(f"capability_changes[{i}] must name a need (slug or id).")
            continue
        if "capability_version_id" not in cc and not _s(cc.get("verdict")):
            escalate(
                f"capability_changes[{i}] must state what the need would use: "
                f"capability_version_id (null for none) and/or verdict."
            )
            continue
        locked = _find_decision(lock, need_ref)
        if locked is None:
            escalate(
                f"'{_echo(need_ref)}' is not a locked capability need. A new capability "
                f"needs its own need, search and decision before it is used."
            )
            continue
        slug, dec = locked
        if slug in lock.drifted_needs:
            escalate(
                f"The decision for '{slug}' has changed since lock v{lock.version}. "
                f"What the lock says about it is out of date; relock before changing "
                f"or relying on it.",
                {"type": "decision", "need_slug": slug, **dec},
            )
            continue
        proposed_cv = _s(cc.get("capability_version_id")) or None
        proposed_verdict = _s(cc.get("verdict")).upper() or None
        same_cv = (
            "capability_version_id" not in cc
            or proposed_cv == (dec.get("capability_version_id") or None)
        )
        same_verdict = proposed_verdict in (None, dec["verdict"])
        if not (same_cv and same_verdict):
            target = dec.get("capability_key") or "no capability (BUILD)"
            escalate(
                f"Locked decision for '{slug}' is {dec['verdict']} of {target}; "
                f"the change proposes "
                f"{_echo(proposed_verdict) if proposed_verdict else 'a different capability'}"
                f"{' ' + _echo(proposed_cv) if proposed_cv else ''}.",
                {"type": "decision", "need_slug": slug, **dec},
            )

    # --- provider changes -----------------------------------------------
    approved = {
        p["name"].lower(): p for p in lock.approved_providers
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    }
    listed = ", ".join(sorted(p["name"] for p in approved.values())) or "none approved"
    for i, pc in items("provider_changes"):
        name = _s(pc.get("name"))
        if not name:
            escalate(f"provider_changes[{i}] must name a provider.")
            continue
        action = _s(pc.get("action")).lower() or "add"
        existing = approved.get(name.lower())
        if action in ("add", "use"):
            if existing is None:
                escalate(
                    f"Provider '{_echo(name)}' is not in the lock's approved providers ({listed})."
                )
        elif action in ("remove", "change", "replace"):
            if existing is not None:
                escalate(
                    f"Provider '{_echo(name)}' is locked as {existing['category']} "
                    f"({existing['cost']}); '{action}' alters a locked choice.",
                    {"type": "provider", **existing},
                )
            else:
                escalate(f"Provider '{_echo(name)}' is not in the lock; cannot {action} it.")
        else:
            escalate(
                f"provider_changes[{i}]: unknown action '{_echo(action)}'; use add, use, "
                f"remove, change or replace."
            )

    # --- new third-party dependencies ------------------------------------
    locked_keys = {
        (d.get("capability_key") or "").lower()
        for d in lock.decisions.values()
        if d.get("capability_key") and d.get("verdict") in _DEPENDENCY_VERDICTS
    }
    reference_keys = {
        (d.get("capability_key") or "").lower()
        for d in lock.decisions.values()
        if d.get("capability_key") and d.get("verdict") == "REFERENCE"
    }
    drifted_keys = {
        (lock.decisions[s].get("capability_key") or "").lower(): s
        for s in lock.drifted_needs
        if s in lock.decisions and lock.decisions[s].get("capability_key")
    }
    for i, dep in items("dependency_adds"):
        if not _s(dep.get("name")) or not _s(dep.get("ecosystem")):
            escalate(f"dependency_adds[{i}] must give ecosystem and name.")
            continue
        key = f"{_s(dep['ecosystem']).lower()}:{_s(dep['name']).lower()}"
        if key in drifted_keys:
            escalate(
                f"Dependency '{_echo(key)}' is the locked choice for '{drifted_keys[key]}', "
                f"whose decision has changed since lock v{lock.version}. Relock first."
            )
        elif key in reference_keys and key not in locked_keys:
            escalate(
                f"Dependency '{_echo(key)}' is locked as REFERENCE: approved to read and "
                f"learn from, not to install. Making it a dependency is a different "
                f"decision (ADOPT, ADAPT or WRAP); re-decide and relock."
            )
        elif key not in locked_keys:
            escalate(
                f"Dependency '{_echo(key)}' is not a locked reuse decision. Search-before-build "
                f"applies to dependencies too: record a capability need, search, decide, relock."
            )

    # --- cross-cutting project decisions ----------------------------------
    locked_choices = {
        d["key"].lower(): d for d in lock.project_decisions
        if isinstance(d, dict) and isinstance(d.get("key"), str)
    }
    for i, pd in items("project_decision_changes"):
        key = _s(pd.get("key")).lower()
        choice = _s(pd.get("choice"))
        if not key or not choice:
            escalate(f"project_decision_changes[{i}] must give key and choice.")
            continue
        existing = locked_choices.get(key)
        if existing is None:
            escalate(
                f"'{_echo(key)}' is not a locked project decision "
                f"({', '.join(sorted(locked_choices)) or 'none locked'}). A new "
                f"cross-cutting choice goes into the lock before it is built on."
            )
        elif existing["choice"].strip().lower() != choice.lower():
            escalate(
                f"Locked project decision '{key}' is '{existing['choice']}'; "
                f"the change proposes '{_echo(choice)}'.",
                {"type": "project_decision", **existing},
            )

    # --- paths ------------------------------------------------------------
    raw_paths = _list(change.get("changed_paths"))
    paths = [p for p in raw_paths if isinstance(p, str)]
    if len(paths) != len(raw_paths):
        escalate(
            "changed_paths must be a list of path strings; the other entries "
            "were not checked."
        )
    if raw_paths:
        if not task_requested:
            escalate(
                "changed_paths needs build_task_id: paths are only permitted "
                "relative to a task."
            )
        elif task is None:
            escalate("build_task_id does not match a task in this project.")
        else:
            title = _echo(task.title)
            if not task.is_current:
                escalate(
                    f"Task '{title}' was created under a superseded lock and its "
                    f"decisions have changed; abandon it and create a new task."
                )
            if task.status in ("abandoned", "verified"):
                escalate(f"Task '{title}' is {task.status}; it should not be receiving changes.")
            if task.drifted_needs:
                escalate(
                    f"Task '{title}' rests on {', '.join(task.drifted_needs)}, whose "
                    f"decision has changed since the lock. Its work is on hold until a relock."
                )
            if not task.permitted_paths:
                escalate(f"Task '{title}' declares no permitted_paths.")
            # A task made before the rules on patterns existed can hold
            # '**'. Such a pattern permits nothing now.
            usable = tuple(p for p in task.permitted_paths if path_pattern_problem(p) is None)
            for bad in (p for p in task.permitted_paths if p not in usable):
                escalate(
                    f"Task '{title}' declares the permitted path {_echo(bad)!r}, which "
                    f"is not accepted (it {path_pattern_problem(bad)}). Abandon the task "
                    f"and create one with real paths."
                )
            for p in paths:
                norm = normalise_path(p)
                if norm is None:
                    escalate(f"Path '{_echo(p)}' is absolute or escapes the repository.")
                elif not usable or not path_permitted(norm, usable):
                    escalate(
                        f"Path '{_echo(p)}' is outside the permitted paths of task "
                        f"'{title}' ({', '.join(usable) or 'none usable'}).",
                        {"type": "task", "build_task_id": task.id,
                         "permitted_paths": list(usable)},
                    )

    reminders = tuple(task.prohibited_changes) if task else ()
    if verdict == ALLOWED:
        reasons.append(
            f"No conflict with lock v{lock.version} in the declared paths, "
            f"capabilities, providers, dependencies or project decisions."
        )
    return LockCheckResult(verdict, tuple(reasons), tuple(cited), reminders)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def normalise_path(path: str) -> str | None:
    """Repo-relative POSIX path, or None if it is absolute or escapes."""
    p = (path or "").strip().replace("\\", "/")
    if not p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    return "/".join(parts)


def path_pattern_problem(pattern: Any) -> str | None:
    """
    Why `pattern` cannot be a task's permitted path, or None if it can.

    It must be repo-relative, stay inside the repository, and start with a
    literal directory or file name. A pattern that begins with a wildcard
    (`*`, `**`, `**/x`) permits the whole repository, which is the same as
    declaring nothing.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return "must be a non-empty string"
    norm = normalise_path(pattern.strip().rstrip("/") or pattern)
    if norm is None:
        return "must be repo-relative and must not contain '..'"
    if any(ch in norm.split("/")[0] for ch in "*?["):
        return (
            "must start with a literal directory or file name; a leading "
            "wildcard would permit the whole repository"
        )
    return None


def path_permitted(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """
    `path` is already normalised. A pattern matches when it is:
      - the exact file
      - a directory (with or without trailing slash) containing the file
      - a glob; `*` and `**` both cross directory separators
    """
    for raw in patterns:
        pat = (raw or "").strip().replace("\\", "/")
        if not pat:
            continue
        while pat.startswith("./"):
            pat = pat[2:]
        directory = pat.rstrip("/")
        if not directory:
            continue
        if path == directory or path.startswith(directory + "/"):
            return True
        if any(ch in pat for ch in "*?[") and fnmatch.fnmatchcase(path, pat.replace("**", "*")):
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_decision(lock: LockView, ref: str) -> tuple[str, dict[str, Any]] | None:
    if ref in lock.decisions:
        return ref, lock.decisions[ref]
    for slug, dec in lock.decisions.items():
        if dec.get("need_id") == ref:
            return slug, dec
    return None


def _echo(v: Any) -> str:
    """Caller-supplied text quoted back in a reason: one line, bounded."""
    return one_line(v, 160)


def _list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""
