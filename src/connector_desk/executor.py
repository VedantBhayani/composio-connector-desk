"""guarded_execute: the only path from the agent to any tool.

policy check -> ledger(decision) -> dry-run stub or real execution -> ledger(result)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from .config import STATUS_TAB, TRACKER_TAB
from .ledger import Ledger
from .sheet_schema import WRITABLE_COLUMNS, TrackerRow
from .tools_policy import (READ_ONLY_TOOLS, SHEET_WRITE_TOOLS, PolicyError, check_actor, check_tool, parse_range,
                           pin_spreadsheet, range_sheet, validate_args)


class Backend(Protocol):
    def execute(self, tool: str, arguments: dict) -> dict[str, Any]:
        """Return {"successful": bool, "data": Any, "error": str | None}."""


class ComposioBackend:
    """Adapter over a Composio session. Only guarded_execute should call it."""

    def __init__(self, session: Any):
        self._session = session

    def execute(self, tool: str, arguments: dict) -> dict[str, Any]:
        res = self._session.execute(tool, arguments=arguments)
        result_type = getattr(res, "result_type", None)
        return {
            "successful": result_type == "completed",
            "data": getattr(res, "data", None),
            "error": getattr(res, "error", None) or (None if result_type == "completed" else result_type),
        }


@dataclass
class RunContext:
    run_id: str
    mode: str  # live | dry_run | local
    today: date
    rows: list[TrackerRow] = field(default_factory=list)
    sheet_id: str = ""
    # Connector_IDs the code matched to an email this run: the only rows the model may write.
    writable_rows: set[str] = field(default_factory=set)
    # Connector_IDs that are stale and untouched this run: the only rows the model may draft to.
    draftable_rows: set[str] = field(default_factory=set)
    # Connector_IDs whose matched email looks like instructions to the agent: stage frozen this run.
    stage_locked_rows: set[str] = field(default_factory=set)
    tracker_tab: str = TRACKER_TAB
    status_tab: str = STATUS_TAB
    drafted: set[tuple[str, str]] = field(default_factory=set)  # (connector_id, iso date)
    changed_rows: set[str] = field(default_factory=set)
    drafts_created: list[str] = field(default_factory=list)
    suspicious_rows: set[str] = field(default_factory=set)

    def row_by_email(self, email: str) -> TrackerRow | None:
        email = (email or "").strip().lower()
        return next((r for r in self.rows if r.email == email), None)

    def row_by_number(self, n: int) -> TrackerRow | None:
        return next((r for r in self.rows if r.row_number == n), None)

    def apply_write(self, tool: str, args: dict) -> None:
        """Mirror an accepted write into the context so summaries reflect it (also in dry run)."""
        if tool == "GMAIL_CREATE_EMAIL_DRAFT":
            row = self.row_by_email(args.get("recipient_email") or "")
            if row:
                self.drafted.add((row.connector_id, self.today.isoformat()))
                self.drafts_created.append(row.connector_id)
        elif tool in SHEET_WRITE_TOOLS and range_sheet(args) == self.tracker_tab:
            row_number = parse_range(args["range"])[2]
            row = self.row_by_number(row_number)
            if row is None:
                return
            cells = dict(zip(WRITABLE_COLUMNS, [str(c) for c in args["values"][0]]))
            row.stage = cells.get("Stage", row.stage)
            if "Last_Touched" in cells:
                row.last_touched = self.today
            row.next_action = cells.get("Next_Action", row.next_action)
            row.notes = cells.get("Notes", row.notes)
            self.changed_rows.add(row.connector_id)
            if "suspicious:" in row.notes.lower():
                self.suspicious_rows.add(row.connector_id)


def guarded_execute(
    tool: str, args: dict | None, *, backend: Backend, ledger: Ledger, ctx: RunContext, actor: str = "model"
) -> dict[str, Any]:
    """Execute one tool call under policy. Raises PolicyError if refused (after logging it).

    actor is "model" for calls the model proposed (the default, and the stricter rules) or
    "code" for the program's own reads and its Status write.
    """
    args = dict(args or {})
    call_id = uuid.uuid4().hex[:10]
    try:
        reason = check_tool(tool)
        check_actor(tool, args, actor, ctx)
        pin_spreadsheet(tool, args, ctx)
        validate_args(tool, args, ctx)
    except PolicyError as exc:
        ledger.record(tool=tool, args=args, decision="blocked", reason=str(exc), result_status="not_executed", call_id=call_id)
        raise

    if ctx.mode == "dry_run" and tool not in READ_ONLY_TOOLS:
        ledger.record(tool=tool, args=args, decision="dry_run", reason=f"{reason}; write stubbed in dry run", result_status="stubbed", call_id=call_id)
        ctx.apply_write(tool, args)
        return {"successful": True, "data": {"dry_run": True, "tool": tool}, "error": None}

    ledger.record(tool=tool, args=args, decision="allowed", reason=reason, result_status="pending", call_id=call_id)
    try:
        result = backend.execute(tool, args)
    except Exception as exc:  # transport/auth errors: log and surface to the agent
        ledger.record(tool=tool, args=None, decision="allowed", reason=f"error: {type(exc).__name__}: {exc}", result_status="error", event="result", call_id=call_id)
        return {"successful": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}
    ok = bool(result.get("successful"))
    ledger.record(tool=tool, args=None, decision="allowed", reason="" if ok else str(result.get("error"))[:200], result_status="ok" if ok else "failed", event="result", call_id=call_id)
    if ok and tool not in READ_ONLY_TOOLS:
        ctx.apply_write(tool, args)
    return result


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]
