import json

from connector_desk.ledger import Ledger, read_ledger

FAKE_KEY = "sk-ant-FAKE-test-key-1234567890"


def test_lines_are_valid_json_with_required_fields(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", "run1", "dry_run")
    led.record(tool="GMAIL_FETCH_EMAILS", args={"query": "x"}, decision="allowed", reason="r", result_status="pending")
    led.record(tool="GMAIL_SEND_EMAIL", args={}, decision="blocked", reason="r", result_status="not_executed")
    lines = (tmp_path / "l.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        e = json.loads(line)
        for field in ("ts", "run_id", "mode", "tool", "args_summary", "decision", "reason", "result_status"):
            assert field in e


def test_append_only_across_instances(tmp_path):
    p = tmp_path / "l.jsonl"
    Ledger(p, "run1", "local").record(tool="T", args={}, decision="allowed", reason="", result_status="ok")
    before = p.read_text()
    Ledger(p, "run2", "local").record(tool="T", args={}, decision="allowed", reason="", result_status="ok")
    after = p.read_text()
    assert after.startswith(before)
    assert [e["run_id"] for e in read_ledger(p)] == ["run1", "run2"]


def test_bodies_truncated_and_secret_keys_redacted(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", "r", "live")
    e = led.record(
        tool="GMAIL_CREATE_EMAIL_DRAFT",
        args={"body": "x" * 500, "access_token": "abc123456", "api_key": "zzz999999"},
        decision="allowed", reason="", result_status="pending",
    )
    assert len(e["args_summary"]["body"]) <= 120 + len("...[truncated]")
    assert e["args_summary"]["access_token"] == "[redacted]"
    assert e["args_summary"]["api_key"] == "[redacted]"


def test_fake_api_key_in_env_never_appears(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("COMPOSIO_API_KEY", FAKE_KEY + "-composio")
    led = Ledger(tmp_path / "l.jsonl", "r", "live")
    led.record(tool="T", args={"note": f"leak {FAKE_KEY}"}, decision="blocked", reason=f"x {FAKE_KEY}", result_status="not_executed")
    assert FAKE_KEY not in (tmp_path / "l.jsonl").read_text()


def test_counts(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", "r", "local")
    led.record(tool="A", args={}, decision="allowed", reason="", result_status="pending")
    led.record(tool="A", args={}, decision="allowed", reason="", result_status="ok", event="result")
    led.record(tool="B", args={}, decision="blocked", reason="", result_status="not_executed")
    c = led.counts()
    assert c["allowed"] == 1 and c["blocked"] == 1 and c["total_decisions"] == 2 and c["executed_ok"] == 1


def test_draft_idempotency_uses_the_runs_local_day_not_the_utc_timestamp(tmp_path):
    """Near midnight the UTC date differs from the local date the run uses as 'today'."""
    from connector_desk.ledger import live_drafts_on

    led = Ledger(tmp_path / "l.jsonl", "r1", "live", day="2030-01-01")
    led.record(tool="GMAIL_CREATE_EMAIL_DRAFT", args={"recipient_email": "Tom@x.example.com"},
               decision="allowed", reason="", result_status="pending", call_id="c1")
    led.record(tool="GMAIL_CREATE_EMAIL_DRAFT", args=None, decision="allowed", reason="",
               result_status="ok", event="result", call_id="c1")
    assert live_drafts_on(led.path, "2030-01-01") == {"tom@x.example.com"}
