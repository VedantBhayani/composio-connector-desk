"""The guard limits WHAT the model may touch, not just which tools it may name.

Each test drives guarded_execute (the only path to a tool) the way the agent loop does:
actor="model" for calls the model proposed, actor="code" for the program's own reads/writes.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from connector_desk.agent import run_agent
from connector_desk.executor import RunContext, guarded_execute
from connector_desk.ledger import Ledger
from connector_desk.local_mode import LocalBackend, load_tracker_grid, local_tool_schemas
from connector_desk.sheet_schema import parse_rows
from connector_desk.tools_policy import PolicyError

TODAY = date(2026, 11, 22)


def setup(tmp_path, mode="live"):
    # As run_agent sets it up: C008 (row 9) and C005 (row 6, Live) were matched to an email.
    ctx = RunContext("r1", mode, TODAY, rows=parse_rows(load_tracker_grid(TODAY), TODAY), sheet_id="sheet-1",
                     writable_rows={"C008", "C005"}, draftable_rows={"C007"})
    backend = LocalBackend(TODAY)
    return ctx, backend, Ledger(tmp_path / "l.jsonl", "r1", mode)


def model(tool, args, ctx, backend, led):
    return guarded_execute(tool, args, backend=backend, ledger=led, ctx=ctx, actor="model")


@pytest.mark.parametrize("tool,args", [
    ("GMAIL_FETCH_EMAILS", {"query": "in:anywhere", "max_results": 500}),
    ("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": "sheet-1", "ranges": ["Tracker!A1:Z999"]}),
])
def test_model_cannot_call_read_tools(tmp_path, tool, args):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="model may not read"):
        model(tool, args, ctx, backend, led)
    assert backend.calls == []
    assert led.entries()[-1]["decision"] == "blocked"


def status_write(**over):
    return {"range": "Status!A1:AX500", "values": [["pwned"] * 50] * 500, **over}


def test_model_cannot_write_status_tab(tmp_path):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="Status tab is written by code only"):
        model("GOOGLESHEETS_VALUES_UPDATE", status_write(), ctx, backend, led)
    assert backend.calls == []


def row_write(row=9, cells=None, **over):
    cells = cells or ["Sandbox credentials", TODAY.isoformat(), "Human review", "Creds coming Friday."]
    return {"range": f"Tracker!E{row}:H{row}", "values": [cells], **over}


def test_write_to_another_spreadsheet_is_rejected(tmp_path):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="spreadsheet_id"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(spreadsheet_id="someone-elses-sheet"), ctx, backend, led)
    assert backend.calls == []


def test_missing_spreadsheet_id_is_filled_with_the_configured_one(tmp_path):
    ctx, backend, led = setup(tmp_path)
    model("GOOGLESHEETS_VALUES_UPDATE", row_write(), ctx, backend, led)
    assert backend.calls[-1][1]["spreadsheet_id"] == "sheet-1"


def test_model_cannot_write_a_row_no_email_matched(tmp_path):
    """One-row-per-call alone still allows a bulk edit, one call at a time (row 10 is C009)."""
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="not matched to an email this run"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(row=10, cells=["Requested", TODAY.isoformat(), "x", "y"]),
              ctx, backend, led)
    assert backend.calls == []


def test_a_matched_row_is_written_at_most_once_per_run(tmp_path):
    ctx, backend, led = setup(tmp_path)
    model("GOOGLESHEETS_VALUES_UPDATE", row_write(), ctx, backend, led)
    with pytest.raises(PolicyError, match="already updated C008 this run"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(), ctx, backend, led)
    assert len(backend.calls) == 1


def test_a_live_row_cannot_be_changed_even_when_matched(tmp_path):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="C005 is Live"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(row=6, cells=["Requested", TODAY.isoformat(), "x", "y"]),
              ctx, backend, led)
    assert backend.calls == []


def test_stage_cannot_move_backwards(tmp_path):
    """An injected 'reset this connector' would otherwise undo weeks of approvals."""
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="backwards"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(cells=["Requested", TODAY.isoformat(), "x", "y"]),
              ctx, backend, led)
    assert backend.calls == []


@pytest.mark.parametrize("cells,msg", [
    (["Sandbox credentials", "2099-01-01", "x", "y"], "Last_Touched must be today"),
    (["Sandbox credentials", "", "x", "y"], "Last_Touched must be today"),
    (["Sandbox credentials", TODAY.isoformat(), "x" * 201, "y"], "Next_Action is too long"),
    (["Sandbox credentials", TODAY.isoformat(), "x", "y" * 301], "Notes is too long"),
])
def test_written_fields_are_validated(tmp_path, cells, msg):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match=msg):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(cells=cells), ctx, backend, led)
    assert backend.calls == []


@pytest.mark.parametrize("to", [
    "dana@acmecrm.example.com",     # C001: a real partner, but it was just matched, not stale
    "ife@harborforms.example.com",  # C008: the partner whose inbox sent the injection
])
def test_drafts_only_go_to_stale_follow_up_rows(tmp_path, to):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="not a stale follow-up candidate"):
        model("GMAIL_CREATE_EMAIL_DRAFT", {"recipient_email": to, "subject": "Hi", "body": "Any update?"},
              ctx, backend, led)
    assert backend.calls == []


@pytest.mark.parametrize("tool,args", [
    # a stale row's address passes the check, the attacker rides along in `to`
    ("GMAIL_CREATE_EMAIL_DRAFT", {"recipient_email": "tom@garnetbilling.example.com", "to": "attacker@evil.example.com",
                                  "subject": "Hi", "body": "Any update?"}),
    # an alias key the pin does not look at
    ("GOOGLESHEETS_VALUES_UPDATE", {**row_write(), "spreadsheetId": "someone-elses-sheet"}),
])
def test_write_tools_reject_unknown_argument_keys(tmp_path, tool, args):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="unexpected argument"):
        model(tool, args, ctx, backend, led)
    assert backend.calls == []


def test_a_stage_only_write_still_needs_all_four_cells(tmp_path):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="all four"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(cells=["Partner verification"]), ctx, backend, led)
    assert backend.calls == []


def test_values_are_written_raw_never_parsed_as_formulas(tmp_path):
    """USER_ENTERED would turn a model-written '=IMPORTXML(...)' into a live formula."""
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="RAW"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(value_input_option="USER_ENTERED"), ctx, backend, led)
    model("GOOGLESHEETS_VALUES_UPDATE", row_write(), ctx, backend, led)
    assert backend.calls[-1][1]["value_input_option"] == "RAW"


@pytest.mark.parametrize("rng", ["Tracker!E9:H10", "Tracker!A9:H9", "Tracker!E9", "Tracker!E9:G9", "E9:H9"])
def test_tracker_range_must_be_exactly_one_row_e_to_h(tmp_path, rng):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(range=rng), ctx, backend, led)
    assert backend.calls == []


SUSPICIOUS = "suspicious: contains instructions to the agent"


def test_a_row_flagged_by_the_code_heuristic_keeps_its_stage(tmp_path):
    """Live run 9135a3c3: the model noted C008 as suspicious but still advanced its stage."""
    ctx, backend, led = setup(tmp_path)
    ctx.stage_locked_rows = {"C008"}
    with pytest.raises(PolicyError, match="stage is locked"):
        model("GOOGLESHEETS_VALUES_UPDATE", row_write(cells=["Partner verification", TODAY.isoformat(), "x", "y"]),
              ctx, backend, led)
    assert backend.calls == []


def test_a_write_that_marks_its_own_row_suspicious_cannot_change_the_stage(tmp_path):
    ctx, backend, led = setup(tmp_path)
    with pytest.raises(PolicyError, match="stage is locked"):
        model("GOOGLESHEETS_VALUES_UPDATE",
              row_write(cells=["Partner verification", TODAY.isoformat(), "Human review: suspicious email", SUSPICIOUS]),
              ctx, backend, led)
    model("GOOGLESHEETS_VALUES_UPDATE",
          row_write(cells=["Sandbox credentials", TODAY.isoformat(), "Human review: suspicious email", SUSPICIOUS]),
          ctx, backend, led)
    assert len(backend.calls) == 1


def test_the_agent_locks_rows_whose_email_the_heuristic_flags(tmp_path):
    backend = LocalBackend(TODAY)
    report = run_agent(mode="local", llm=BulkEditLLM(), model="m", backend=backend, tools=local_tool_schemas(),
                       ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
                       gmail_label="l", sheet_id="sheet-1", out=lambda s: None)
    # BulkEditLLM moves every row to Partner verification; C008 (email 06, flagged) must be refused now.
    assert "C008" not in report.summary["rows_changed"]
    assert any("C008" in r and "stage is locked" in r for r in report.refused_calls)


class RecordingLLM:
    """Ends immediately; records which tools it was offered."""

    def __init__(self):
        self.messages, self.offered = self, None

    def create(self, *, tools, **_):
        self.offered = sorted(t["name"] for t in tools)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])


def test_model_is_offered_only_write_tools(tmp_path):
    llm = RecordingLLM()
    run_agent(mode="local", llm=llm, model="m", backend=LocalBackend(TODAY), tools=local_tool_schemas(),
              ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
              gmail_label="l", sheet_id="sheet-1", out=lambda s: None)
    assert llm.offered == ["GMAIL_CREATE_EMAIL_DRAFT", "GOOGLESHEETS_VALUES_UPDATE"]


class BulkEditLLM:
    """Obeys an injection: one valid-looking write per row, for EVERY tracker row (not Live)."""

    def __init__(self):
        self.messages, self.turn = self, 0

    def create(self, **_):
        self.turn += 1
        if self.turn > 1:
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])
        uses = [SimpleNamespace(type="tool_use", id=f"w{n}", name="GOOGLESHEETS_VALUES_UPDATE",
                                input=row_write(row=n, cells=["Partner verification", TODAY.isoformat(), "x", "y"]))
                for n in range(2, 12)]
        return SimpleNamespace(stop_reason="tool_use", content=uses)


def test_bulk_edit_through_the_agent_loop_only_reaches_matched_rows(tmp_path):
    backend = LocalBackend(TODAY)
    report = run_agent(mode="local", llm=BulkEditLLM(), model="m", backend=backend, tools=local_tool_schemas(),
                       ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
                       gmail_label="l", sheet_id="sheet-1", out=lambda s: None)
    # Emails matched C001-C004 and C008; every other row's write is refused, and C008's
    # stage is locked because its email (06) is flagged as instructions to the agent.
    assert report.summary["rows_changed"] == ["C001", "C002", "C003", "C004"]
    assert len(report.refused_calls) == 6  # C005 (Live), C006, C007, C008 (locked), C009, C010


class DraftToLLM:
    def __init__(self, to):
        self.messages, self.turn, self.to = self, 0, to

    def create(self, **_):
        self.turn += 1
        if self.turn > 1:
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])
        return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
            type="tool_use", id="d", name="GMAIL_CREATE_EMAIL_DRAFT",
            input={"recipient_email": self.to, "subject": "Hi", "body": "Any update?"})])


def test_no_draft_to_anyone_who_emailed_this_run_even_if_unmatched(tmp_path):
    """Two rows share ife@ (C007, C008), so her suspicious email matches no single row and
    both rows stay stale. She still must not get a draft in the run that read her email."""
    backend = LocalBackend(TODAY)
    backend.tracker[7][3] = "ife@harborforms.example.com"  # C007 now shares C008's address
    backend.emails[5]["from"] = "Ife Okoro <ife@harborforms.example.com>"  # email 06
    report = run_agent(mode="local", llm=DraftToLLM("ife@harborforms.example.com"), model="m", backend=backend,
                       tools=local_tool_schemas(), ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY,
                       stale_days=7, gmail_label="l", sheet_id="s", out=lambda s: None)
    assert backend.drafts == []
    assert "not a stale follow-up candidate" in report.refused_calls[0]


def test_model_never_sees_the_fields_the_system_fills_in(tmp_path):
    """Composio's live schema marks value_input_option required, so a model fills it in
    (live dry run: USER_ENTERED, refused). The model's copy of the schema drops both fields."""
    live_like = {"name": "GOOGLESHEETS_VALUES_UPDATE", "description": "Update values.", "input_schema": {
        "type": "object", "required": ["spreadsheet_id", "range", "value_input_option", "values"],
        "properties": {k: {"type": "string"} for k in ("spreadsheet_id", "range", "value_input_option", "values")}}}
    llm = RecordingLLM()
    llm.create_orig = llm.create

    def capture(*, tools, **kw):
        llm.schemas = tools
        return llm.create_orig(tools=tools, **kw)

    llm.create = capture
    run_agent(mode="local", llm=llm, model="m", backend=LocalBackend(TODAY), tools=[live_like],
              ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
              gmail_label="l", sheet_id="sheet-1", out=lambda s: None)
    schema = llm.schemas[0]["input_schema"]
    assert set(schema["properties"]) == {"range", "values"}
    assert schema["required"] == ["range", "values"]
    assert live_like["input_schema"]["required"][0] == "spreadsheet_id"  # the original is not mutated


def test_code_can_write_status_tab(tmp_path):
    ctx, backend, led = setup(tmp_path)
    guarded_execute("GOOGLESHEETS_VALUES_UPDATE", status_write(range="Status!A1:B1", values=[["Run", "r1"]]),
                    backend=backend, ledger=led, ctx=ctx, actor="code")
    assert backend.status == [["Run", "r1"]]
