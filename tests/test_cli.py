from datetime import date

from connector_desk.agent import run_agent
from connector_desk.cli import main
from connector_desk.ledger import Ledger
from connector_desk.local_mode import LocalBackend, ScriptedLLM, local_tool_schemas

READ_ONLY = {"GMAIL_FETCH_EMAILS", "GOOGLESHEETS_BATCH_GET"}


def test_run_local_prints_mode_summary_ledger_and_not_tested(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "l.jsonl"))
    assert main(["run", "--local"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("LOCAL MODE")
    for marker in ("STATUS SUMMARY", "LEDGER SUMMARY", "NOT TESTED LIVE"):
        assert marker in out


def test_live_run_without_keys_exits_cleanly(tmp_path, monkeypatch, capsys):
    for k in ("COMPOSIO_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "COMPOSIO_USER_ID", "SHEET_ID"):
        monkeypatch.setenv(k, "")
    assert main(["run", "--dry-run"]) == 2
    assert "missing in .env" in capsys.readouterr().err


def test_dry_run_mode_reads_but_never_writes(tmp_path):
    today = date(2026, 11, 22)
    backend = LocalBackend(today)  # stands in for the live session
    led = Ledger(tmp_path / "l.jsonl", "d1", "dry_run")
    report = run_agent(mode="dry_run", llm=ScriptedLLM(), model="x", backend=backend, tools=local_tool_schemas(),
                       ledger=led, today=today, stale_days=7, gmail_label="l", sheet_id="s", out=lambda s: None)
    assert {t for t, _ in backend.calls} <= READ_ONLY
    assert backend.drafts == [] and backend.status == []
    assert report.summary["drafts_created"] == ["C007"]  # projected, not executed
    c = led.counts()
    assert c["dry_run"] == 7  # 5 row updates + 1 draft + 1 status write, all stubbed
    assert all(e["decision"] in ("allowed", "dry_run") for e in led.entries())


def test_sample_csv(tmp_path, monkeypatch):
    out = tmp_path / "t.csv"
    assert main(["sample-csv", "--out", str(out)]) == 0
    text = out.read_text()
    assert "today-" not in text and "C010" in text
