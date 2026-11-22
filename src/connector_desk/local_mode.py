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
