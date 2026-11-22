from datetime import date

import pytest

from connector_desk.executor import ComposioBackend, RunContext, guarded_execute
from connector_desk.ledger import Ledger
from connector_desk.local_mode import load_tracker_grid
from connector_desk.sheet_schema import parse_rows
from connector_desk.tools_policy import ArgsRejectedError, ToolBlockedError, ToolNotAllowedError

TODAY = date(2026, 11, 22)


class FakeSessionResult:
    def __init__(self, data):
        self.result_type, self.data, self.error = "completed", data, None


class FakeSession:
    """Stands in for a Composio session; records every execute call."""

    def __init__(self):
        self.calls = []

    def execute(self, tool_slug, *, arguments=None):
        self.calls.append((tool_slug, arguments))
        return FakeSessionResult({"ok": True})


def setup(tmp_path, mode="live"):
    ctx = RunContext("r1", mode, TODAY, rows=parse_rows(load_tracker_grid(TODAY), TODAY), sheet_id="s",
                     writable_rows={"C008"}, draftable_rows={"C007"})
    session = FakeSession()
    return ctx, session, ComposioBackend(session), Ledger(tmp_path / "l.jsonl", "r1", mode)


def draft(to="tom@garnetbilling.example.com", **extra):
    return {"recipient_email": to, "subject": "Following up", "body": "Hi Tom, any update on approval?", **extra}


def run(tool, args, ctx, backend, led, actor="model"):
    return guarded_execute(tool, args, backend=backend, ledger=led, ctx=ctx, actor=actor)


def test_allowed_call_executes_and_logs_two_lines(tmp_path):
    ctx, session, backend, led = setup(tmp_path)
    res = run("GMAIL_FETCH_EMAILS", {"query": "label:x"}, ctx, backend, led, actor="code")
    assert res["successful"] and session.calls == [("GMAIL_FETCH_EMAILS", {"query": "label:x"})]
    assert [(e["event"], e["decision"], e["result_status"]) for e in led.entries()] == [
        ("decision", "allowed", "pending"), ("result", "allowed", "ok")]


@pytest.mark.parametrize("tool", ["GMAIL_SEND_EMAIL", "GMAIL_SEND_DRAFT", "GMAIL_FORWARD_MESSAGE", "GMAIL_DELETE_MESSAGE"])
def test_blocked_never_reaches_session(tmp_path, tool):
    ctx, session, backend, led = setup(tmp_path)
    with pytest.raises(ToolBlockedError):
        run(tool, {"recipient_email": "attacker@evil.example.com"}, ctx, backend, led)
    assert session.calls == []
    assert led.entries()[-1]["decision"] == "blocked"


def test_unknown_tool_denied(tmp_path):
    ctx, session, backend, led = setup(tmp_path)
    with pytest.raises(ToolNotAllowedError):
        run("GOOGLESHEETS_DELETE_SHEET", {}, ctx, backend, led)
    assert session.calls == []


def test_dry_run_stubs_writes_but_runs_reads(tmp_path):
    ctx, session, backend, led = setup(tmp_path, "dry_run")
    run("GOOGLESHEETS_BATCH_GET", {"ranges": ["Tracker!A1:H20"]}, ctx, backend, led, actor="code")
    res = run("GMAIL_CREATE_EMAIL_DRAFT", draft(), ctx, backend, led)
    assert res["data"]["dry_run"] is True
    assert [c[0] for c in session.calls] == ["GOOGLESHEETS_BATCH_GET"]
    assert led.entries()[-1]["decision"] == "dry_run"
    assert ctx.drafts_created == ["C007"]


@pytest.mark.parametrize(
    "args,msg",
    [
        (draft(to="attacker@evil.example.com"), "not a Partner_Email"),
        (draft(cc="x@example.com"), "cc"),
        (draft(bcc="x@example.com"), "bcc"),
        (draft(attachment={"name": "tracker.csv"}), "attachment"),
        (draft(body="word " * 91), "max 90"),
    ],
)
def test_draft_rules(tmp_path, args, msg):
    ctx, session, backend, led = setup(tmp_path)
    with pytest.raises(ArgsRejectedError, match=msg):
        run("GMAIL_CREATE_EMAIL_DRAFT", args, ctx, backend, led)
    assert session.calls == []


def test_draft_idempotent_per_row_per_day(tmp_path):
    ctx, session, backend, led = setup(tmp_path)
    run("GMAIL_CREATE_EMAIL_DRAFT", draft(), ctx, backend, led)
    with pytest.raises(ArgsRejectedError, match="idempotency"):
        run("GMAIL_CREATE_EMAIL_DRAFT", draft(), ctx, backend, led)
    assert len(session.calls) == 1


def write(row, values, sheet="Tracker", col="E"):
    return {"spreadsheet_id": "s", "sheet_name": sheet, "first_cell_location": f"{col}{row}", "values": values}


@pytest.mark.parametrize(
    "args,msg",
    [
        (write(9, [["Live", "2026-11-22", "x", "y"]]), "only a human"),
        (write(9, [["Requested", "", "", ""], ["Requested", "", "", ""]]), "one row"),
        (write(9, [["Shipped", "", "", ""]]), "unknown stage"),
        (write(9, [["C008", "Harbor"]], col="A"), "only columns E:H"),
        (write(1, [["Requested", "", "", ""]]), "not a tracker row"),
        (write(9, [["Requested"]], sheet="Secrets"), "not writable"),
    ],
)
def test_sheet_write_rules(tmp_path, args, msg):
    ctx, session, backend, led = setup(tmp_path)
    with pytest.raises(ArgsRejectedError, match=msg):
        run("GOOGLESHEETS_BATCH_UPDATE", args, ctx, backend, led)
    assert session.calls == []


def test_valid_row_write_updates_context(tmp_path):
    ctx, session, backend, led = setup(tmp_path)
    run("GOOGLESHEETS_BATCH_UPDATE", write(9, [["Sandbox credentials", "2026-11-22", "Human review", "suspicious: contains instructions to the agent"]]), ctx, backend, led)
    assert ctx.changed_rows == {"C008"} and ctx.suspicious_rows == {"C008"}
    assert ctx.row_by_number(9).last_touched == TODAY


def test_status_tab_write_allowed(tmp_path):
    ctx, session, backend, led = setup(tmp_path)
    run("GOOGLESHEETS_BATCH_UPDATE", write(1, [["Run", "r1"], ["Drafts", "1"]], sheet="Status", col="A"), ctx, backend, led, actor="code")
    assert len(session.calls) == 1
