"""The agent loop: Claude (or a stand-in) proposes tool calls; every call goes through guarded_execute."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .config import STATUS_TAB, TRACKER_TAB
from .executor import Backend, RunContext, guarded_execute
from .ledger import Ledger
from .local_mode import looks_like_agent_instructions
from .prompts import SYSTEM_PROMPT, build_user_message
from .sheet_schema import extract_emails, extract_values, is_stale, match_email, parse_rows, stage_counts
from .tools_policy import ALLOWED_TOOLS, READ_ONLY_TOOLS, PolicyError

MAX_TURNS = 12
MAX_TOOL_CALLS = 40
TOOL_RESULT_CHARS = 4000


class RunAborted(RuntimeError):
    pass


@dataclass
class RunReport:
    run_id: str
    mode: str
    summary: dict[str, Any]
    final_text: str = ""
    refused_calls: list[str] = field(default_factory=list)


def filter_tools(tools: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Keep only allow-listed tools from what the session exposes. Returns (kept, dropped, missing)."""
    names = [t.get("name", "") for t in tools]
    kept = [t for t in tools if t.get("name") in ALLOWED_TOOLS]
    dropped = sorted(n for n in names if n not in ALLOWED_TOOLS)
    missing = sorted(set(ALLOWED_TOOLS) - set(names))
    return kept, dropped, missing


def _block_to_dict(block: Any) -> dict[str, Any]:
    get = block.get if isinstance(block, dict) else lambda k, d=None: getattr(block, k, d)
    if get("type") == "tool_use":
        return {"type": "tool_use", "id": get("id"), "name": get("name"), "input": get("input") or {}}
    return {"type": "text", "text": get("text") or ""}


def run_agent(
    *,
    mode: str,
    llm: Any,
    model: str,
    backend: Backend,
    tools: list[dict],
    ledger: Ledger,
    today: date,
    stale_days: int,
    gmail_label: str,
    sheet_id: str,
    already_drafted: set[str] | None = None,
    out: Callable[[str], None] = print,
) -> RunReport:
    ctx = RunContext(run_id=ledger.run_id, mode=mode, today=today, sheet_id=sheet_id)

    def call(tool: str, args: dict, actor: str = "code") -> dict:
        return guarded_execute(tool, args, backend=backend, ledger=ledger, ctx=ctx, actor=actor)

    # 1. Read inputs in code (ledgered like every other call).
    fetched = call("GMAIL_FETCH_EMAILS", {"query": f"label:{gmail_label}", "max_results": 25,
                                         "verbose": True, "include_payload": True})  # bodies, not metadata only
    if not fetched.get("successful"):
        raise RunAborted(f"could not fetch emails: {fetched.get('error')}")
    emails = extract_emails(fetched.get("data"))
    warnings = read_warnings(emails, gmail_label)
    for w in warnings:
        out(f"WARNING: {w}")
    sheet = call("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": sheet_id, "ranges": [f"{TRACKER_TAB}!A1:H200"]})
    if not sheet.get("successful"):
        raise RunAborted(f"could not read tracker: {sheet.get('error')}")
    ctx.rows = parse_rows(extract_values(sheet.get("data")), today)
    for r in ctx.rows:  # live idempotency across runs: one draft per row per day
        if r.email in (already_drafted or set()):
            ctx.drafted.add((r.connector_id, today.isoformat()))
    drafted_ids = {cid for cid, _ in ctx.drafted}

    matches = {e.id: match_email(e, ctx.rows) for e in emails}
    matched_ids = {m.row.connector_id for m in matches.values() if m.row}
    ctx.writable_rows = set(matched_ids)
    # Never follow up with someone whose email was just read, matched or not (e.g. a shared
    # Partner_Email made the match ambiguous): a human reads their reply first.
    senders = {e.sender for e in emails if e.sender}
    stale = [r.connector_id for r in ctx.rows if is_stale(r, today, stale_days)
             and r.connector_id not in matched_ids | drafted_ids and r.email not in senders]
    ctx.draftable_rows = set(stale)
    heuristic_flags = [e.id for e in emails if looks_like_agent_instructions(e.body + " " + e.subject)]
    ctx.stage_locked_rows = {matches[eid].row.connector_id for eid in heuristic_flags if matches[eid].row}

    run_data = {
        "today": today.isoformat(),
        "stale_days": stale_days,
        "tracker": [r.as_dict() for r in ctx.rows],
        "emails": [
            {
                "id": e.id, "from": e.sender, "subject": e.subject, "date": e.date, "body": e.body[:4000],
                "code_match": ({"connector_id": m.row.connector_id, "row_number": m.row.row_number, "method": m.method}
                               if (m := matches[e.id]).row else None),
            }
            for e in emails
        ],
        "stale_candidates": stale,
    }

    # 2. Model loop. The model only proposes; guarded_execute decides. Code already did the
    # reads, so the model is offered the write tools only.
    model_tools = [model_tool_schema(t) for t in tools if t.get("name") not in READ_ONLY_TOOLS]
    messages: list[dict] = [{"role": "user", "content": build_user_message(run_data)}]
    final_text, refused, n_calls = "", [], 0
    for _ in range(MAX_TURNS):
        try:
            resp = llm.messages.create(model=model, max_tokens=4096, system=SYSTEM_PROMPT, tools=model_tools, messages=messages)
        except Exception as exc:  # provider/network error: stop cleanly, ledger and footer still print
            raise RunAborted(f"model call failed: {type(exc).__name__}: {exc}") from exc
        blocks = [_block_to_dict(b) for b in resp.content]
        messages.append({"role": "assistant", "content": blocks})
        uses = [b for b in blocks if b["type"] == "tool_use"]
        if not uses:
            final_text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
            if getattr(resp, "stop_reason", None) == "max_tokens":
                warnings.append("the model's final reply was cut off at the token limit")
                out(f"WARNING: {warnings[-1]}")
            break
        results = []
        for u in uses:
            n_calls += 1
            if n_calls > MAX_TOOL_CALLS:
                raise RunAborted(f"model exceeded {MAX_TOOL_CALLS} tool calls")
            try:
                r = call(u["name"], u["input"], actor="model")
                content, is_error = json.dumps(r, default=str)[:TOOL_RESULT_CHARS], not r.get("successful")
            except PolicyError as exc:
                refused.append(f"{u['name']}: {exc}")
                content, is_error = f"REFUSED BY POLICY: {exc}", True
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    else:
        out(f"warning: stopped after {MAX_TURNS} model turns")

    # 3. Status summary, computed in code from what actually happened.
    summary = {
        "date": today.isoformat(),
        "run_id": ledger.run_id,
        "mode": mode,
        "emails_read": len(emails),
        "stage_counts": stage_counts(ctx.rows),
        "rows_changed": sorted(ctx.changed_rows),
        "drafts_created": ctx.drafts_created,
        "suspicious_rows": sorted(ctx.suspicious_rows),
        "suspicious_emails_heuristic": heuristic_flags,
        "needs_human": [f"{eid}: {m.note}" for eid, m in matches.items() if not m.row],
        "refused_tool_calls": len(refused),
        "warnings": warnings,
    }
    if mode != "local":
        try:
            grid = status_grid(summary)
            call("GOOGLESHEETS_VALUES_UPDATE", {
                "spreadsheet_id": sheet_id, "range": f"{STATUS_TAB}!A1:B{len(grid)}",
                "values": grid,
            })
        except PolicyError as exc:  # should not happen; logged by the executor
            out(f"status write refused: {exc}")
    return RunReport(ledger.run_id, mode, summary, final_text, refused)


# Filled in by the executor (pin_spreadsheet). Hidden from the model so it does not guess them.
SYSTEM_FILLED_ARGS = frozenset({"spreadsheet_id", "value_input_option"})


def model_tool_schema(tool: dict) -> dict:
    """A copy of the tool definition without the arguments the system fills in."""
    schema = dict(tool.get("input_schema") or {})
    schema["properties"] = {k: v for k, v in (schema.get("properties") or {}).items() if k not in SYSTEM_FILLED_ARGS}
    if "required" in schema:
        schema["required"] = [k for k in schema["required"] if k not in SYSTEM_FILLED_ARGS]
    return {**tool, "input_schema": schema}


def read_warnings(emails: list, gmail_label: str) -> list[str]:
    """Loud signs that the fetch 'succeeded' but read nothing useful (wrong label, a
    metadata-only response, or a changed response shape)."""
    if not emails:
        return [f"fetched 0 emails under label:{gmail_label}; check the label and the fetch response shape"]
    warnings = []
    for name in ("body", "sender"):
        empty = [e.id or "?" for e in emails if not getattr(e, name).strip()]
        if empty:
            warnings.append(f"emails with no {name}: {', '.join(empty)} (metadata-only fetch or changed response shape?)")
    return warnings


def status_grid(s: dict[str, Any]) -> list[list[str]]:
    grid = [["Connector Desk status", s["date"]], ["Run id", s["run_id"]], ["Mode", s["mode"]], ["Stage", "Count"]]
    grid += [[stage, str(n)] for stage, n in s["stage_counts"].items()]
    grid += [
        ["Rows changed", ", ".join(s["rows_changed"]) or "none"],
        ["Drafts created", ", ".join(s["drafts_created"]) or "none"],
        ["Suspicious emails", ", ".join(s["suspicious_rows"]) or "none"],
        ["Needs human", "; ".join(s["needs_human"]) or "none"],
        ["Refused tool calls", str(s["refused_tool_calls"])],
        ["Warnings", "; ".join(s["warnings"]) or "none"],
    ]
    return grid


def format_summary(s: dict[str, Any]) -> str:
    lines = [f"STATUS SUMMARY ({s['mode']}, {s['date']}, run {s['run_id']})", f"  emails read: {s['emails_read']}", "  stage counts:"]
    lines += [f"    {k}: {v}" for k, v in s["stage_counts"].items()]
    lines += [
        f"  rows changed: {', '.join(s['rows_changed']) or 'none'}",
        f"  drafts created: {', '.join(s['drafts_created']) or 'none'}",
        f"  suspicious (noted on row): {', '.join(s['suspicious_rows']) or 'none'}",
        f"  suspicious (code heuristic): {', '.join(s['suspicious_emails_heuristic']) or 'none'}",
        f"  needs human: {'; '.join(s['needs_human']) or 'none'}",
        f"  refused tool calls: {s['refused_tool_calls']}",
        f"  warnings: {'; '.join(s['warnings']) or 'none'}",
    ]
    return "\n".join(lines)


def create_live_session(api_key: str, user_id: str) -> tuple[Any, list[dict], list[str], list[str]]:
    """Composio session restricted to the allow-list. Returns (session, tools, dropped, missing).

    Imports are local so offline tests and --local never need composio installed or a key.
    """
    from composio import Composio
    from composio_anthropic import AnthropicProvider

    from .tools_policy import build_session_config

    composio = Composio(api_key=api_key, provider=AnthropicProvider())
    # composio 0.34 exposes both `composio.sessions.create` and the `composio.create` shortcut.
    create = composio.sessions.create if hasattr(composio, "sessions") else composio.create
    session = create(**build_session_config(user_id))
    tools, dropped, missing = filter_tools(list(session.tools()))
    return session, tools, dropped, missing
