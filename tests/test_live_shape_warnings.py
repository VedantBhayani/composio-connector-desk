"""A live run that silently reads nothing must not look like a clean run.

Composio's GMAIL_FETCH_EMAILS has a reported failure mode where it returns `messages: []`
with success, and a metadata-only mode that returns no bodies. Both must be loud.
"""
from datetime import date

from connector_desk.agent import run_agent
from connector_desk.ledger import Ledger
from connector_desk.local_mode import LocalBackend, ScriptedLLM, local_tool_schemas

TODAY = date(2026, 11, 22)


def run_with(tmp_path, emails):
    backend = LocalBackend(TODAY)
    backend.emails = emails
    printed = []
    report = run_agent(mode="local", llm=ScriptedLLM(), model="m", backend=backend, tools=local_tool_schemas(),
                       ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
                       gmail_label="connector-desk", sheet_id="s", out=printed.append)
    return report, printed


def test_zero_emails_is_a_warning(tmp_path):
    report, printed = run_with(tmp_path, [])
    assert any("0 emails" in w and "connector-desk" in w for w in report.summary["warnings"])
    assert any(line.startswith("WARNING") for line in printed)


def test_emails_without_body_or_sender_are_a_warning(tmp_path):
    emails = [{"id": "m1", "from": "", "subject": "Re: creds", "body": "", "date": "2026-11-22"}]
    report, _ = run_with(tmp_path, emails)
    assert any("m1" in w and "body" in w for w in report.summary["warnings"])
    assert any("m1" in w and "sender" in w for w in report.summary["warnings"])


def test_clean_fixture_run_has_no_warnings(tmp_path):
    report, printed = run_with(tmp_path, LocalBackend(TODAY).emails)
    assert report.summary["warnings"] == []
    assert not any(line.startswith("WARNING") for line in printed)
