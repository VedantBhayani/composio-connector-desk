"""Append-only JSONL action ledger. One line per tool decision (plus one per result)."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SECRET_ENV_VARS

MAX_FIELD = 120
_SECRET_KEY = re.compile(r"token|secret|password|api[_-]?key|authorization|credential", re.I)


def _truncate(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD else value[:MAX_FIELD] + "...[truncated]"
    if isinstance(value, dict):
        return {k: ("[redacted]" if _SECRET_KEY.search(str(k)) else _truncate(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_truncate(v) for v in list(value)[:10]]
        if len(value) > 10:
            items.append(f"...[{len(value) - 10} more]")
        return items
    return value


def summarize_args(args: dict | None) -> dict:
    return _truncate(dict(args or {}))


def _scrub(line: str) -> str:
    """Remove any secret env value that slipped into the line."""
    for name in SECRET_ENV_VARS:
        val = os.environ.get(name)
        if val and len(val) >= 6:
            line = line.replace(val, "[redacted]")
    return line


class Ledger:
    def __init__(self, path: Path | str, run_id: str, mode: str):
        self.path = Path(path)
        self.run_id = run_id
        self.mode = mode
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        tool: str,
        args: dict | None,
        decision: str,
        reason: str,
        result_status: str,
        event: str = "decision",
        call_id: str = "",
    ) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "mode": self.mode,
            "event": event,
            "call_id": call_id,
            "tool": tool,
            "args_summary": summarize_args(args),
            "decision": decision,
            "reason": reason,
            "result_status": result_status,
        }
        line = _scrub(json.dumps(entry, ensure_ascii=False, default=str))
        with self.path.open("a", encoding="utf-8") as fh:  # append-only
            fh.write(line + "\n")
        return json.loads(line)

    def entries(self, this_run_only: bool = True) -> list[dict]:
        rows = read_ledger(self.path)
        return [r for r in rows if r["run_id"] == self.run_id] if this_run_only else rows

    def counts(self) -> dict[str, int]:
        decisions = [e for e in self.entries() if e.get("event") == "decision"]
        results = [e for e in self.entries() if e.get("event") == "result"]
        c = Counter(e["decision"] for e in decisions)
        c["total_decisions"] = len(decisions)
        c["executed_ok"] = sum(1 for e in results if e["result_status"] == "ok")
        c["executed_failed"] = sum(1 for e in results if e["result_status"] != "ok")
        return dict(c)


def read_ledger(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def live_drafts_on(path: Path | str, day: str) -> set[str]:
    """Recipients of live drafts that succeeded on `day` (YYYY-MM-DD, UTC), for idempotency."""
    entries = read_ledger(path)
    ok_calls = {e["call_id"] for e in entries if e.get("event") == "result" and e["result_status"] == "ok"}
    return {
        str(e["args_summary"].get("recipient_email", "")).lower()
        for e in entries
        if e.get("event") == "decision" and e["mode"] == "live" and e["tool"] == "GMAIL_CREATE_EMAIL_DRAFT"
        and e["call_id"] in ok_calls and e["ts"][:10] == day
    }
