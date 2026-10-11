"""
The human-only commands: `cip-composer approvals` and `cip-composer verify`.

`approvals approve|reject` is the only code path that records a human
decision. It must refuse a non-interactive caller, and it must make the
human type a code taken from the content being approved.

`verify` runs an acceptance test itself and records what it observed, so
the evidence does not rest on the agent's word.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from cip_composer import cli
from db.connection import connect
from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q
from tests.pdr_helpers import (
    bespoke_task,
    locked_build,
    record_need,
    scalar,
    with_requirements,
)


@pytest.fixture
def cli_db(monkeypatch, conn):
    """Point the CLI at the test database."""
    monkeypatch.setattr(cli, "connect", lambda: connect(for_tests=True))
    return conn


def _pending_request(conn) -> dict:
    ctx = with_requirements(conn)
    record_need(conn, ctx.pid)
    return q.request_requirements_confirmation(conn, project_id=ctx.pid)


def _code(conn, req: dict) -> str:
    """The content code the human is asked to type: from the request's own hash."""
    return scalar(conn, "SELECT subject_hash FROM approval_request WHERE id = %s",
                  (req["approval_request_id"],))[:6]


def _interactive(monkeypatch, answers: list[str]) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------

def test_parser_wires_the_human_commands():
    p = cli._build_parser()
    assert p.parse_args(["approvals", "list"]).func is cli.cmd_approvals_list
    assert p.parse_args(["approvals", "show", "abc"]).func is cli.cmd_approvals_show
    assert p.parse_args(["approvals", "approve", "abc"]).func is cli.cmd_approvals_approve
    assert p.parse_args(["approvals", "reject", "abc"]).func is cli.cmd_approvals_reject
    ns = p.parse_args(["verify", "--task", "t", "--criterion", "1", "--req", "r",
                       "--", "pytest", "-q"])
    assert ns.func is cli.cmd_verify and ns.command[-2:] == ["pytest", "-q"]
    assert p.parse_args(["pdr-status", "clinic"]).func is cli.cmd_pdr_status


def test_approve_refuses_a_non_interactive_caller(cli_db, monkeypatch, capsys):
    """An agent's shell is not a terminal. Nothing may be recorded."""
    req = _pending_request(cli_db)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt"))
    rc = cli.main(["approvals", "approve", req["approval_request_id"]])
    assert rc == 3
    assert "interactive terminal" in capsys.readouterr().out
    assert scalar(cli_db, "SELECT count(*) FROM approval") == 0


def test_approve_as_a_subprocess_with_no_terminal(cli_db):
    """The same refusal, end to end, the way an agent would actually hit it."""
    req = _pending_request(cli_db)
    proc = subprocess.run(
        [sys.executable, "-m", "cip_composer.cli", "approvals", "approve",
         req["approval_request_id"]],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 3 and "interactive terminal" in proc.stdout
    assert scalar(cli_db, "SELECT count(*) FROM approval") == 0


def test_approve_needs_the_content_code(cli_db, monkeypatch, capsys):
    req = _pending_request(cli_db)
    _interactive(monkeypatch, ["yes"])
    assert cli.main(["approvals", "approve", req["approval_request_id"]]) == 1
    assert "did not match" in capsys.readouterr().out
    assert scalar(cli_db, "SELECT count(*) FROM approval") == 0


def test_approve_records_the_os_user(cli_db, monkeypatch, capsys):
    req = _pending_request(cli_db)
    _interactive(monkeypatch, [_code(cli_db, req)])
    monkeypatch.setattr(cli, "_human_identity", lambda: "ricky@4rsquared")
    assert cli.main(["approvals", "approve", req["approval_request_id"][:8]]) == 0
    out = capsys.readouterr().out
    assert "Requirements confirmation" in out          # the human saw what they approved
    with cli_db.cursor() as cur:
        cur.execute("SELECT outcome, approver, channel FROM approval")
        assert cur.fetchone() == ("approved", "ricky@4rsquared", "cli_tty")
    # And the gate it guards is now open.
    again = q.request_requirements_confirmation(
        cli_db, project_id=scalar(cli_db, "SELECT project_id FROM approval_request"))
    assert again["status"] == "approved" and again["approved_by"] == "ricky@4rsquared"


def test_reject_asks_what_to_change(cli_db, monkeypatch):
    req = _pending_request(cli_db)
    _interactive(monkeypatch, [_code(cli_db, req), "dark mode is in scope"])
    assert cli.main(["approvals", "reject", req["approval_request_id"]]) == 0
    with cli_db.cursor() as cur:
        cur.execute("SELECT outcome, note FROM approval")
        assert cur.fetchone() == ("rejected", "dark mode is in scope")


def test_cannot_decide_twice(cli_db, monkeypatch, capsys):
    req = _pending_request(cli_db)
    _interactive(monkeypatch, [_code(cli_db, req)])
    assert cli.main(["approvals", "approve", req["approval_request_id"]]) == 0
    _interactive(monkeypatch, [_code(cli_db, req)])
    assert cli.main(["approvals", "approve", req["approval_request_id"]]) == 1
    assert "Already approved" in capsys.readouterr().out


def test_list_and_show(cli_db, capsys):
    assert cli.main(["approvals", "list"]) == 0
    assert "Nothing is waiting" in capsys.readouterr().out
    req = _pending_request(cli_db)
    assert cli.main(["approvals", "list"]) == 0
    assert req["approval_request_id"][:8] in capsys.readouterr().out
    assert cli.main(["approvals", "show", req["approval_request_id"]]) == 0
    assert "requirements_confirmation" in capsys.readouterr().out
    assert cli.main(["approvals", "show", "ffffffff"]) == 1


def test_a_request_whose_content_does_not_match_its_hash_cannot_be_approved(cli_db):
    """
    An approval is for the content shown, checked against its hash at the
    moment of approval. The update rule already stops a request being
    edited in place; this covers a row that was written wrong, or altered
    by someone with database access.
    """
    ctx = with_requirements(cli_db)
    with cli_db.cursor() as cur:
        cur.execute(
            "INSERT INTO approval_request (project_id, kind, subject_hash, subject, summary) "
            "VALUES (%s, 'requirements_confirmation', %s, %s::jsonb, 'looks fine') "
            "RETURNING id",
            (ctx.pid, "0" * 64, '{"schema": "requirements/1", "swapped": true}'),
        )
        request_id = str(cur.fetchone()[0])
    cli_db.commit()
    res = pdr_approvals.record_approval(
        cli_db, request_id=request_id, outcome="approved", approver="ricky@desk")
    assert "does not match its hash" in res["error"]
    assert scalar(cli_db, "SELECT count(*) FROM approval") == 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

@pytest.fixture
def target_repo(tmp_path, monkeypatch):
    """A throwaway git repository standing in for the app being built."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "ok.py").write_text("print('fine')\n")
    (tmp_path / "bad.py").write_text("import sys; sys.exit(3)\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    return tmp_path, head


def _implemented_task(conn):
    ctx = locked_build(conn)
    tid = bespoke_task(conn, ctx)["build_task_id"]
    q.record_build_progress(conn, build_task_id=tid, status="in_progress")
    q.record_build_progress(conn, build_task_id=tid, status="implemented")
    return ctx, tid


def test_verify_runs_the_command_and_records_what_it_saw(cli_db, target_repo, capsys):
    _, head = target_repo
    ctx, tid = _implemented_task(cli_db)
    rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                   "--test-ref", "ok.py", "--", sys.executable, "ok.py"])
    assert rc == 0 and "PASS: todays-bookings #1" in capsys.readouterr().out
    with cli_db.cursor() as cur:
        cur.execute("SELECT outcome, recorded_by, commit_sha, exit_code, worktree_dirty, "
                    "test_ref, length(output_digest) FROM requirement_verification")
        assert cur.fetchone() == ("pass", "cip_executed", head, 0, False, "ok.py", 64)
    assert q.record_build_progress(cli_db, build_task_id=tid,
                                   status="verified")["status"] == "verified"
    assert q.coverage(cli_db, project_id=ctx.pid)["progress"]["verified_cip_executed"] == 1


def test_verify_records_a_failing_command_as_a_fail(cli_db, target_repo, capsys):
    ctx, tid = _implemented_task(cli_db)
    rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                   "--", sys.executable, "bad.py"])
    assert rc == 2
    with cli_db.cursor() as cur:
        cur.execute("SELECT outcome, exit_code, recorded_by FROM requirement_verification")
        assert cur.fetchone() == ("fail", 3, "cip_executed")
    out = capsys.readouterr().out
    assert "FAIL" in out and "still unverified" in out


def test_verify_notes_a_dirty_tree(cli_db, target_repo):
    repo, _ = target_repo
    (repo / "ok.py").write_text("print('edited but not committed')\n")
    ctx, tid = _implemented_task(cli_db)
    cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
              "--", sys.executable, "ok.py"])
    assert scalar(cli_db, "SELECT worktree_dirty FROM requirement_verification") is True


def test_verify_needs_a_commit_to_point_at(cli_db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                          # not a git repository
    ctx, tid = _implemented_task(cli_db)
    rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                   "--", sys.executable, "-c", "pass"])
    assert rc == 1 and "git repository" in capsys.readouterr().out
    assert scalar(cli_db, "SELECT count(*) FROM requirement_verification") == 0


def test_verify_refuses_a_criterion_the_task_does_not_deliver(cli_db, target_repo, capsys):
    ctx, tid = _implemented_task(cli_db)
    rc = cli.main(["verify", "--task", tid, "--req", "staff-sign-in", "--criterion", "1",
                   "--", sys.executable, "ok.py"])
    assert rc == 1 and "does not belong" in capsys.readouterr().out
    assert scalar(cli_db, "SELECT count(*) FROM requirement_verification") == 0


def test_status_command_prints_the_next_actions(cli_db, capsys):
    ctx = locked_build(cli_db)
    assert cli.main(["pdr-status", "test-project"]) == 0
    out = capsys.readouterr().out
    assert "Phase:   building" in out and "Lock: v1 approved by ricky@desk (usable)" in out
    assert cli.main(["pdr-status", "no-such-project"]) == 1
