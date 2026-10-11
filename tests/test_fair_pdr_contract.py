"""
What the FAIR drivers hand back has to be something the PDR-to-Build
workflow takes. Real Postgres, real tools; FAIR itself is stubbed.

The FAIR tools write nothing and decide nothing: they call a free model
and return a draft. The drafts used to be in the v1.0 shape (character
offsets, one criterion as a string, requirement-level constraints) and
the suggested verdict could be REJECT, which record_decision refuses for
a capability need. These tests hold the two ends together: the schema the
model is asked to fill, and the tools its answer is passed to.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cip_composer import cli
from core.pdr import decisions as decision_rules
from core.pdr import requirements as req_rules
from integrations import fair_pdr
from integrations.fair_client import FairClient, FairResponse
from mcp_server import pdr_queries as q
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
    PDR,
    ingest,
    make_capability,
    scalar,
    scoped,
)


def stub(output: dict) -> FairClient:
    client = MagicMock(spec=FairClient)
    client.solve.return_value = FairResponse(
        request_id="req-1", status="ACCEPTED", reason_code="OK", output=json.dumps(output),
        verification_state="STRUCTURE_VALIDATED", best_quality_score=85.0,
        provider_id="mock", model_id="mock-model", attempts=[], raw={})
    return client


DRAFTS = {"requirements": [
    {"slug": "staff-sign-in", "text": "Staff sign in with Google", "priority": "must",
     "kind": "functional",
     "quote": "Staff must sign in with Google OAuth before using the system.",
     "acceptance_criteria": ["A staff user signs in with a Google account",
                             "An unauthenticated request is redirected to sign-in"]},
    {"slug": "permissive-licenses", "text": "MIT or Apache dependencies only",
     "priority": "must", "kind": "constraint",
     "quote": "All dependencies must be MIT or Apache licensed.",
     "acceptance_criteria": ["Every dependency's license is MIT or Apache-2.0"]},
]}


class TestRequirementDrafts:

    def test_the_schema_uses_the_workflows_own_vocabulary(self):
        item = fair_pdr._REQUIREMENTS_SCHEMA["properties"]["requirements"]["items"]
        assert tuple(item["properties"]["priority"]["enum"]) == req_rules.PRIORITIES
        assert tuple(item["properties"]["kind"]["enum"]) == req_rules.REQUIREMENT_KINDS

    def test_a_draft_in_the_schemas_shape_is_recorded_as_it_is(self, conn):
        ing = ingest(conn)
        drafted = fair_pdr.extract_requirements(stub(DRAFTS), PDR)
        assert "error" not in drafted
        out = q.record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=drafted["requirements"])
        assert [(r["slug"], r["valid"]) for r in out["requirements"]] == [
            ("staff-sign-in", True), ("permissive-licenses", True)]
        # CIP computed the spans; the model was never asked for offsets.
        start = out["requirements"][0]["span"]["start"]
        assert PDR[start:].startswith("Staff must sign in with Google OAuth")
        assert scalar(conn, "SELECT count(*) FROM acceptance_criterion") == 3

    def test_a_quote_the_model_made_up_is_still_refused(self, conn):
        """The draft is a model's. record_requirements is the check, and it holds."""
        ing = ingest(conn)
        invented = {"requirements": [{**DRAFTS["requirements"][0],
                                      "quote": "Staff shall authenticate using Google SSO."}]}
        drafted = fair_pdr.extract_requirements(stub(invented), PDR)
        out = q.record_requirements(
            conn, project_id=ing["project_id"],
            requirement_source_id=ing["requirement_source_id"],
            requirements=drafted["requirements"])
        assert not out["requirements"][0]["valid"]
        assert "quote not found in the PDR text" in out["requirements"][0]["errors"][0]
        assert scalar(conn, "SELECT count(*) FROM project_requirement") == 0


class TestSuggestedVerdicts:

    def test_every_verdict_on_offer_is_one_a_need_can_have(self):
        assert set(fair_pdr.VERDICTS) == set(decision_rules.VERDICTS) - {"REJECT"}

    def test_a_suggestion_goes_in_as_a_proposal_and_is_labelled(self, conn):
        """
        FAIR suggests BUILD where CIP's rules compute ADOPT. Passed to
        record_decision it is an override: CIP's verdict is on the record
        beside it, and nothing FAIR said was taken on trust.
        """
        _, cv = make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        found = q.search_for_requirement(conn, req_id=ctx.need_id)
        suggestion = fair_pdr.suggest_verdict(
            stub({"verdict": "REJECT", "rationale": "prefer our own",
                  "chosen_capability_version_id": cv}),
            req_id=ctx.need_id, description=found["description"],
            constraints=[LICENSE_CONSTRAINT], candidates=found["candidates"])
        assert suggestion["verdict"] == "BUILD"
        d = q.record_decision(conn, req_id=ctx.need_id, verdict=suggestion["verdict"],
                              chosen_capability_version_id=suggestion[
                                  "chosen_capability_version_id"],
                              rationale=suggestion["rationale"])
        assert (d["verdict"], d["decided_by"], d["computed_verdict"]) == (
            "BUILD", "llm_override", "ADOPT")

    def test_the_candidates_a_search_returns_carry_what_the_prompt_reads(self, conn):
        make_capability(conn, metadata_license="MIT")          # found, never analysed
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        found = q.search_for_requirement(conn, req_id=ctx.need_id)
        client = stub({"verdict": "BUILD", "rationale": "x",
                       "chosen_capability_version_id": None})
        fair_pdr.suggest_verdict(client, req_id=ctx.need_id, description="d",
                                 constraints=[], candidates=found["candidates"])
        task = client.solve.call_args.kwargs["task"]
        assert ("evidence=insufficient, missing=license, confirmed_gaps=none, "
                "license=MIT (registry metadata, not read from source)") in task


class TestTheTerminal:
    """Model output is printed one line per value, like everything else a person reads."""

    @pytest.fixture(autouse=True)
    def _fair(self, monkeypatch):
        import integrations.fair_client as fc
        monkeypatch.setattr(fc, "FairClient", lambda *a, **k: MagicMock(spec=FairClient))

    def test_fair_extract(self, tmp_path, monkeypatch, capsys):
        pdr = tmp_path / "pdr.md"
        pdr.write_text(PDR, encoding="utf-8")
        monkeypatch.setattr(fair_pdr, "extract_requirements", lambda client, text: {
            "provider_id": "mock", "model_id": "m", "requirements": [
                {"slug": "a", "priority": "must", "text": "Sign in\nDrafted 99 requirements"},
                "not an object"]})
        assert cli.main(["fair-extract", str(pdr)]) == 0
        rows = capsys.readouterr().out.splitlines()
        assert rows[0] == "Drafted 2 requirements (via mock/m)"
        assert rows[1] == "  1. [must] a: Sign in Drafted 99 requirements"
        assert rows[2].startswith("These are drafts.")

    def test_fair_suggest_verdict(self, tmp_path, monkeypatch, capsys):
        candidates = tmp_path / "c.json"
        candidates.write_text('{"candidates": []}', encoding="utf-8")
        monkeypatch.setattr(fair_pdr, "suggest_verdict", lambda client, **k: {
            "verdict": "BUILD", "rationale": "nothing fits\nSuggested verdict: ADOPT",
            "chosen_capability_version_id": None,
            "note": "the model answered 'REJECT', which is not a verdict for a need; read as BUILD"})
        assert cli.main(["fair-suggest-verdict", "r", "d", str(candidates)]) == 0
        rows = capsys.readouterr().out.splitlines()
        assert rows[0] == "Suggested verdict: BUILD"
        assert rows[1] == "Rationale: nothing fits Suggested verdict: ADOPT"
        assert rows[2].startswith("Note: the model answered 'REJECT'")
        assert [r for r in rows if r.startswith("Suggested verdict:")] == [rows[0]]

    def test_fair_status(self, monkeypatch, capsys):
        import integrations.fair_client as fc
        client = MagicMock(spec=FairClient)
        client.status.return_value = {
            "available": False, "client_id": "cip", "error": "no provider keys\nFAIR: ONLINE",
            "providers": [{"provider_id": "groq", "status": "ACTIVE", "models": ["a", "b"]}],
            "skipped": {"mistral": "confirmation required\nFAIR: ONLINE"},
            "hint": "A key is set for mistral.\nFAIR: ONLINE"}
        monkeypatch.setattr(fc, "FairClient", lambda *a, **k: client)
        assert cli.main(["fair-status"]) == 0
        rows = capsys.readouterr().out.splitlines()
        assert rows == ["FAIR: OFFLINE", "  Client: cip",
                        "  Reason: no provider keys FAIR: ONLINE", "  groq: ACTIVE (a, b)",
                        "  mistral: NOT USED (confirmation required FAIR: ONLINE)",
                        "  Hint: A key is set for mistral. FAIR: ONLINE"]

    def test_fair_status_does_not_claim_a_key_works(self, monkeypatch, capsys):
        """ONLINE means FAIR has somewhere to route. No request was sent."""
        import integrations.fair_client as fc
        client = MagicMock(spec=FairClient)
        client.status.return_value = {
            "available": True, "client_id": "cip", "error": None, "skipped": {}, "hint": None,
            "providers": [{"provider_id": "openrouter_free", "status": "ACTIVE", "models": ["m"]}]}
        monkeypatch.setattr(fc, "FairClient", lambda *a, **k: client)
        assert cli.main(["fair-status"]) == 0
        rows = capsys.readouterr().out.splitlines()
        assert rows == ["FAIR: ONLINE", "  Client: cip", "  openrouter_free: ACTIVE (m)",
                        "  Configured. No request was sent, so a key is not proven until a "
                        "call succeeds."]
