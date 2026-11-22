"""Deny-by-default tool policy. The single source of truth for what the agent may call.

Slugs follow the brief. They could NOT be checked against docs.composio.dev from the
build environment (host blocked); in live mode the agent compares them with what the
Composio session actually exposes and warns on any mismatch.
"""

from __future__ import annotations

import re
from typing import Any

# The ONLY tools the agent may execute. tool slug -> one-line reason.
ALLOWED_TOOLS: dict[str, str] = {
    "GMAIL_FETCH_EMAILS": "read partner emails under GMAIL_LABEL (read-only)",
    "GMAIL_CREATE_EMAIL_DRAFT": "create follow-up drafts; drafts are never sent",
    "GOOGLESHEETS_BATCH_GET": "read the tracker (read-only)",
    "GOOGLESHEETS_BATCH_UPDATE": "update one tracker row per call, or the Status tab",
}

READ_ONLY_TOOLS = frozenset({"GMAIL_FETCH_EMAILS", "GOOGLESHEETS_BATCH_GET"})
SHEET_WRITE_TOOLS = frozenset({"GOOGLESHEETS_BATCH_UPDATE"})

# Explicitly blocked: raise a clear error and are logged as "blocked".
BLOCKED_TOOLS = frozenset(
    {
        "GMAIL_SEND_EMAIL",
        "GMAIL_SEND_DRAFT",
        "GMAIL_FORWARD_MESSAGE",
        "GMAIL_DELETE_MESSAGE",
    }
)

# Asked of Composio as a second layer (experimental SDK feature). These tools are not
# enabled in the session at all, so this only matters if the allow-list is ever widened.
REQUIRE_APPROVAL_TOOLS = ["GMAIL_SEND_EMAIL", "GMAIL_SEND_DRAFT", "GMAIL_FORWARD_MESSAGE"]

# Names that must never appear in ALLOWED_TOOLS (checked by tests).
DANGEROUS_NAME = re.compile(
    r"SEND|FORWARD|DELETE|TRASH|SETTING|FILTER|SHARE|PERMISSION|VACATION|MODIFY_LABEL"
)

TOOLKITS = ("gmail", "googlesheets")

# Checked by `connector_desk schemas` alongside the allow/block lists. Composio's Sheets
# guide marks GOOGLESHEETS_BATCH_UPDATE deprecated in favour of this one.
REPLACEMENT_CANDIDATES = ("GOOGLESHEETS_VALUES_UPDATE",)


def schema_check_slugs() -> list[str]:
    return sorted(ALLOWED_TOOLS) + list(REPLACEMENT_CANDIDATES) + sorted(BLOCKED_TOOLS)


class PolicyError(PermissionError):
    """Base class: the call was refused by our own policy and never executed."""


class ToolBlockedError(PolicyError):
    pass


class ToolNotAllowedError(PolicyError):
    pass


class ArgsRejectedError(PolicyError):
    pass


def toolkit_of(tool: str) -> str:
    if tool.startswith("GMAIL_"):
        return "gmail"
    if tool.startswith("GOOGLESHEETS_"):
        return "googlesheets"
    return "unknown"


def check_tool(tool: str) -> str:
    """Return the allow reason, or raise. Deny by default."""
    if tool in BLOCKED_TOOLS:
        raise ToolBlockedError(
            f"BLOCKED: {tool} is on the block list. Connector Desk never sends, "
            "forwards or deletes email."
        )
    if tool not in ALLOWED_TOOLS:
        raise ToolNotAllowedError(f"DENIED: {tool} is not on the allow-list (deny by default).")
    return ALLOWED_TOOLS[tool]


def pin_spreadsheet(tool: str, args: dict, ctx: Any) -> None:
    """Every Sheets call targets the configured spreadsheet. Fills it in when absent (the
    model is never told the id), rejects any other id. Mutates args."""
    if toolkit_of(tool) != "googlesheets":
        return
    if not ctx.sheet_id:
        raise ArgsRejectedError("sheet call rejected: no spreadsheet_id configured for this run")
    given = args.get("spreadsheet_id")
    if given not in (None, "", ctx.sheet_id):
        raise ArgsRejectedError(f"sheet call rejected: spreadsheet_id {given!r} is not the configured tracker")
    args["spreadsheet_id"] = ctx.sheet_id


ACTORS = ("model", "code")


def check_actor(tool: str, args: dict, actor: str, ctx: Any) -> None:
    """Rules that depend on WHO proposed the call. The code already did both reads, so the
    model gets write tools only; that keeps an injected 'read the whole inbox' from running."""
    if actor not in ACTORS:
        raise ToolNotAllowedError(f"DENIED: unknown actor {actor!r}")
    if actor == "model" and tool in READ_ONLY_TOOLS:
        raise ToolNotAllowedError(f"DENIED: the model may not read; {tool} is run by code only.")
    if actor == "model" and tool in SHEET_WRITE_TOOLS and args.get("sheet_name") == ctx.status_tab:
        raise ArgsRejectedError("sheet write rejected: the Status tab is written by code only")


def build_session_config(user_id: str) -> dict[str, Any]:
    """kwargs for composio.create(...): only gmail + googlesheets, only allow-listed tools."""
    tools: dict[str, dict[str, list[str]]] = {}
    for kit in TOOLKITS:
        tools[kit] = {"enable": sorted(t for t in ALLOWED_TOOLS if toolkit_of(t) == kit)}
    tools["gmail"]["require_approval"] = list(REQUIRE_APPROVAL_TOOLS)
    return {
        "user_id": user_id,
        "toolkits": list(TOOLKITS),
        "tools": tools,
        "session_preset": "direct_tools",
        "sandbox": {"enable": False},  # no remote code execution / bash tools
        "manage_connections": False,  # connections are made once via `connector_desk connect`
    }


# ---------------------------------------------------------------------------
# Argument rules (enforced in code, independent of the prompt)
# ---------------------------------------------------------------------------

DRAFT_MAX_WORDS = 90
_EXTRA_RECIPIENT_KEYS = ("cc", "bcc", "extra_recipients", "attachment", "attachments")


def _first(args: dict, *keys: str) -> Any:
    for k in keys:
        if args.get(k) not in (None, ""):
            return args[k]
    return None


# The only argument keys a write may carry. Anything else (`to`, `spreadsheetId`, ...) is
# refused, so a field the guard does not check can never reach Composio.
WRITE_ARG_KEYS: dict[str, frozenset[str]] = {
    "GMAIL_CREATE_EMAIL_DRAFT": frozenset({"recipient_email", "subject", "body"}),
    "GOOGLESHEETS_BATCH_UPDATE": frozenset({"spreadsheet_id", "sheet_name", "first_cell_location", "values"}),
}


def validate_args(tool: str, args: dict, ctx: Any) -> None:
    """Raise ArgsRejectedError if the call breaks a hard rule. ctx is executor.RunContext."""
    if tool == "GMAIL_CREATE_EMAIL_DRAFT":
        for k in _EXTRA_RECIPIENT_KEYS:  # clearer message for the common case
            if args.get(k):
                raise ArgsRejectedError(f"draft rejected: '{k}' is not allowed (no CC/BCC/attachments)")
    unexpected = sorted(set(args) - WRITE_ARG_KEYS.get(tool, set(args)))
    if unexpected:
        raise ArgsRejectedError(f"{tool} rejected: unexpected argument(s) {', '.join(unexpected)}")
    if tool == "GMAIL_CREATE_EMAIL_DRAFT":
        _validate_draft(args, ctx)
    elif tool == "GOOGLESHEETS_BATCH_UPDATE":
        _validate_sheet_write(args, ctx)


def _validate_draft(args: dict, ctx: Any) -> None:
    recipient = args.get("recipient_email")
    if not isinstance(recipient, str):
        raise ArgsRejectedError("draft rejected: exactly one recipient_email is required")
    row = ctx.row_by_email(recipient)
    if row is None:
        raise ArgsRejectedError(
            f"draft rejected: {recipient!r} is not a Partner_Email in the tracker"
        )
    if row.connector_id not in ctx.draftable_rows:
        raise ArgsRejectedError(f"draft rejected: {row.connector_id} is not a stale follow-up candidate this run")
    body = str(args.get("body", ""))
    words = len(body.split())
    if words > DRAFT_MAX_WORDS:
        raise ArgsRejectedError(f"draft rejected: body is {words} words (max {DRAFT_MAX_WORDS})")
    key = (row.connector_id, ctx.today.isoformat())
    if key in ctx.drafted:
        raise ArgsRejectedError(
            f"draft rejected: already drafted for {row.connector_id} today (idempotency)"
        )


_CELL = re.compile(r"^([A-Z]+)(\d+)$")
NEXT_ACTION_MAX = 200
NOTES_MAX = 300


def _validate_sheet_write(args: dict, ctx: Any) -> None:
    from .sheet_schema import STAGES

    sheet = _first(args, "sheet_name")
    if sheet == ctx.status_tab:
        return
    if sheet != ctx.tracker_tab:
        raise ArgsRejectedError(f"sheet write rejected: tab {sheet!r} is not writable")
    m = _CELL.match(str(_first(args, "first_cell_location") or ""))
    if not m:
        raise ArgsRejectedError("sheet write rejected: first_cell_location like 'E5' required")
    col, row_number = m.group(1), int(m.group(2))
    values = args.get("values")
    if not (isinstance(values, list) and len(values) == 1 and isinstance(values[0], list)):
        raise ArgsRejectedError("sheet write rejected: exactly one row per call")
    cells = values[0]
    if col != "E" or len(cells) > 4:
        raise ArgsRejectedError(
            "sheet write rejected: only columns E:H (Stage, Last_Touched, Next_Action, Notes)"
        )
    if len(cells) != 4:
        raise ArgsRejectedError(
            "sheet write rejected: all four cells required (Stage, Last_Touched, Next_Action, Notes)"
        )
    row = ctx.row_by_number(row_number)
    if row is None:
        raise ArgsRejectedError(f"sheet write rejected: row {row_number} is not a tracker row")
    if row.connector_id not in ctx.writable_rows:
        raise ArgsRejectedError(
            f"sheet write rejected: {row.connector_id} was not matched to an email this run"
        )
    if row.connector_id in ctx.changed_rows:
        raise ArgsRejectedError(f"sheet write rejected: already updated {row.connector_id} this run")
    if row.stage == "Live":
        raise ArgsRejectedError(f"sheet write rejected: {row.connector_id} is Live; only a human changes it")
    stage = str(cells[0]).strip()
    if stage not in STAGES:
        raise ArgsRejectedError(f"sheet write rejected: unknown stage {stage!r}")
    if stage == "Live":
        raise ArgsRejectedError("sheet write rejected: only a human may mark a connector Live")
    if STAGES.index(stage) < STAGES.index(row.stage):
        raise ArgsRejectedError(
            f"sheet write rejected: {row.connector_id} cannot move backwards from {row.stage!r} to {stage!r}"
        )
    if len(cells) >= 2 and str(cells[1]).strip() != ctx.today.isoformat():
        raise ArgsRejectedError(f"sheet write rejected: Last_Touched must be today ({ctx.today.isoformat()})")
    for name, limit, value in (("Next_Action", NEXT_ACTION_MAX, cells[2:3]), ("Notes", NOTES_MAX, cells[3:4])):
        if value and len(str(value[0])) > limit:
            raise ArgsRejectedError(f"sheet write rejected: {name} is too long (max {limit} characters)")
