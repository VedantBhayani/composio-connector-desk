"""Local mode: fixtures stand in for Gmail and Sheets. No network, no keys."""

from __future__ import annotations

import csv
import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, STATUS_TAB, TRACKER_TAB
from .sheet_schema import COLUMNS, parse_date

FIXTURES = REPO_ROOT / "fixtures"


def load_tracker_grid(today: date, path: Path | None = None) -> list[list[str]]:
    """Header + rows with 'today-N' tokens resolved to ISO dates."""
    path = path or FIXTURES / "tracker_sample.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        grid = [row for row in csv.reader(fh)]
    idx = COLUMNS.index("Last_Touched")
    for row in grid[1:]:
        d = parse_date(row[idx], today)
        row[idx] = d.isoformat() if d else ""
    return grid


def load_email_fixtures(today: date, directory: Path | None = None) -> list[dict[str, Any]]:
    directory = directory or FIXTURES / "emails"
    out = []
    for p in sorted(directory.glob("*.json")):
        e = json.loads(p.read_text(encoding="utf-8"))
        d = parse_date(e.get("date", ""), today)
        e["date"] = d.isoformat() if d else ""
        out.append(e)
    return out


def write_sample_csv(today: date, dest: Path) -> Path:
    """Resolved CSV for importing into the throwaway Google Sheet."""
    with dest.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(load_tracker_grid(today))
    return dest


# Heuristic used by the scripted local planner and as a code-level second opinion in
# the run summary. It is NOT the safety boundary: the executor is.
_INJECTION_PATTERNS = [
    r"\b(ai|assistant|agent|llm|language model)\b.{0,40}\b(read|reading|note|instruction)",
    r"ignore (all |any )?(of )?(your |the )?(previous |prior )?(rules|instructions)",
    r"\bforward\b.{0,60}\b(tracker|emails?|inbox|sheet)\b",
    r"mark (every|all) (row|rows|connector)",
    r"(reveal|share|send).{0,30}(api key|secret|password|token|credentials? to)",
    r"do not mention this",
]


def looks_like_agent_instructions(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t, re.S) for p in _INJECTION_PATTERNS)


class LocalBackend:
    """In-memory Gmail + Sheets. Records every call it receives in `self.calls`."""

    def __init__(self, today: date):
        self.today = today
        self.tracker = load_tracker_grid(today)
        self.status: list[list[Any]] = []
        self.emails = load_email_fixtures(today)
        self.drafts: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool: str, arguments: dict) -> dict[str, Any]:
        self.calls.append((tool, deepcopy(arguments)))
        if tool == "GMAIL_FETCH_EMAILS":
            return {"successful": True, "data": {"messages": deepcopy(self.emails)}, "error": None}
        if tool == "GOOGLESHEETS_BATCH_GET":
            rng = f"{TRACKER_TAB}!A1:H{len(self.tracker)}"
            return {"successful": True, "data": {"valueRanges": [{"range": rng, "values": deepcopy(self.tracker)}]}, "error": None}
        if tool == "GOOGLESHEETS_BATCH_UPDATE":
            return self._write(arguments)
        if tool == "GMAIL_CREATE_EMAIL_DRAFT":
            self.drafts.append(deepcopy(arguments))
            return {"successful": True, "data": {"id": f"local-draft-{len(self.drafts)}"}, "error": None}
        return {"successful": False, "data": None, "error": f"local backend has no tool {tool}"}

    def _write(self, args: dict) -> dict[str, Any]:
        m = re.fullmatch(r"([A-Z]+)(\d+)", str(args.get("first_cell_location", "A1")))
        col = ord(m.group(1)[0]) - ord("A") if m else 0
        row0 = int(m.group(2)) - 1 if m else 0
        grid = self.status if args.get("sheet_name") == STATUS_TAB else self.tracker
        for r, values in enumerate(args.get("values") or []):
            while len(grid) <= row0 + r:
                grid.append([])
            line = grid[row0 + r]
            while len(line) < col + len(values):
                line.append("")
            line[col : col + len(values)] = [str(v) for v in values]
        return {"successful": True, "data": {"updatedRows": len(args.get("values") or [])}, "error": None}


# ---------------------------------------------------------------------------
# Deterministic stand-in for Claude in --local mode (offline, no API key).
# It drives the SAME agent loop and executor as the real model. It is a demo of the
# plumbing and the guard, not evidence of how the real model behaves.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from .prompts import parse_run_data  # noqa: E402
from .sheet_schema import STAGES, SUSPICIOUS_NOTE  # noqa: E402

_ASKS = {
    "Requested": "Could you confirm that you received our OAuth request?",
    "OAuth app submitted": "Could you share when you expect to make a decision on our OAuth app?",
    "Sandbox credentials": "Could you let us know when the sandbox credentials will be available?",
    "Partner verification": "Could you tell us what you still need from us to complete verification?",
}


def _classify(body: str, stage: str) -> tuple[str, str, str]:
    """(new_stage, next_action, note) from simple keyword rules. Never Live, never backwards."""
    b = body.lower()
    if "approved" in b and "sandbox" in b:
        new, nxt, note = "Sandbox credentials", "Wait for sandbox credentials", "OAuth app approved for sandbox; credentials to follow."
    elif "sandbox" in b and "created" in b:
        new, nxt, note = "Sandbox credentials", "Test sandbox and confirm it works", "Sandbox created; partner asks us to confirm it works."
    elif "verification" in b and "demo video" in b:
        new, nxt, note = "Partner verification", "Send demo video and data-handling summary", "Security team needs demo video and data-handling summary."
    elif "redirect uri" in b:
        new, nxt, note = stage, "Send redirect URI and scope list", "Request received; partner needs redirect URI and scope list."
    else:
        new, nxt, note = stage, "Human review: unclassified reply", "needs human: could not classify reply."
    if STAGES.index(new) < STAGES.index(stage) or new == "Live":
        new = stage
    return new, nxt, note


def _tool_use(i: int, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"local_{i}", name=name, input=args)


class ScriptedLLM:
    """Mimics client.messages.create(): plans all writes on turn 1, then ends."""

    def __init__(self):
        self.messages = self
        self.turn = 0

    def create(self, *, messages: list[dict], **_: Any) -> SimpleNamespace:
        self.turn += 1
        if self.turn > 1:
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Local planner finished.")])
        data = parse_run_data(messages[0]["content"])
        rows = {r["Connector_ID"]: r for r in data["tracker"]}
        uses, skip_draft = [], set()
        for e in data["emails"]:
            if not e["code_match"]:
                continue  # ambiguous: change nothing
            row = rows[e["code_match"]["connector_id"]]
            if looks_like_agent_instructions(e["subject"] + " " + e["body"]):
                cells = [row["Stage"], data["today"], "Human review: suspicious email", SUSPICIOUS_NOTE]
                skip_draft.add(row["Connector_ID"])
            else:
                stage, nxt, note = _classify(e["body"], row["Stage"])
                cells = [stage, data["today"], nxt, note]
            uses.append(_tool_use(len(uses), "GOOGLESHEETS_BATCH_UPDATE", {
                "sheet_name": "Tracker",  # spreadsheet_id is filled in by the executor
                "first_cell_location": f"E{row['row_number']}", "values": [cells]}))
        for cid in data["stale_candidates"]:
            if cid in skip_draft:
                continue
            r = rows[cid]
            first = r["Partner_Contact_Name"].split()[0]
            body = (f"Hi {first},\n\nI hope you are well. I am following up on the {r['App']} connector, "
                    f"which is currently at the \"{r['Stage']}\" step. {_ASKS[r['Stage']]}\n\n"
                    "Thank you for your help.\n\nBest regards,\nProduct Ops")
            uses.append(_tool_use(len(uses), "GMAIL_CREATE_EMAIL_DRAFT", {
                "recipient_email": r["Partner_Email"], "subject": f"Following up: {r['App']} connector", "body": body}))
        return SimpleNamespace(stop_reason="tool_use", content=uses)


def local_tool_schemas() -> list[dict]:
    """Anthropic tool definitions for local mode (live mode uses session.tools())."""
    s = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    return [
        {"name": "GMAIL_FETCH_EMAILS", "description": "Fetch emails matching a Gmail query.",
         "input_schema": s({"query": {"type": "string"}, "max_results": {"type": "integer"}}, [])},
        {"name": "GMAIL_CREATE_EMAIL_DRAFT", "description": "Create a Gmail draft (never sent).",
         "input_schema": s({"recipient_email": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                           ["recipient_email", "subject", "body"])},
        {"name": "GOOGLESHEETS_BATCH_GET", "description": "Read ranges from the spreadsheet.",
         "input_schema": s({"spreadsheet_id": {"type": "string"}, "ranges": {"type": "array", "items": {"type": "string"}}}, ["ranges"])},
        {"name": "GOOGLESHEETS_BATCH_UPDATE", "description": "Write a block of values starting at a cell.",
         "input_schema": s({"spreadsheet_id": {"type": "string"}, "sheet_name": {"type": "string"},
                            "first_cell_location": {"type": "string"}, "values": {"type": "array", "items": {"type": "array"}}},
                           ["sheet_name", "first_cell_location", "values"])},
    ]
