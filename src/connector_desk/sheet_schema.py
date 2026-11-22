"""Tracker schema, parsing, stale detection and email-to-row matching (pure functions)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from typing import Any

COLUMNS = [
    "Connector_ID", "App", "Partner_Contact_Name", "Partner_Email",
    "Stage", "Last_Touched", "Next_Action", "Notes",
]
STAGES = ["Requested", "OAuth app submitted", "Sandbox credentials", "Partner verification", "Live"]
# Columns the agent may write, in order (E:H).
WRITABLE_COLUMNS = ["Stage", "Last_Touched", "Next_Action", "Notes"]

NEEDS_HUMAN_AMBIGUOUS = "needs human: ambiguous match"
SUSPICIOUS_NOTE = "suspicious: contains instructions to the agent"


class SheetFormatError(ValueError):
    pass


@dataclass
class TrackerRow:
    row_number: int  # 1-based sheet row (header is row 1)
    connector_id: str
    app: str
    contact: str
    email: str
    stage: str
    last_touched: date | None
    next_action: str
    notes: str

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number, "Connector_ID": self.connector_id, "App": self.app,
            "Partner_Contact_Name": self.contact, "Partner_Email": self.email, "Stage": self.stage,
            "Last_Touched": self.last_touched.isoformat() if self.last_touched else "",
            "Next_Action": self.next_action, "Notes": self.notes,
        }


def parse_date(value: str, today: date | None = None) -> date | None:
    """ISO date, or a fixture token like 'today-12' resolved against `today`."""
    value = (value or "").strip()
    if not value:
        return None
    m = re.fullmatch(r"today(?:-(\d+))?", value)
    if m:
        if today is None:
            raise SheetFormatError("relative date needs a reference 'today'")
        return today - timedelta(days=int(m.group(1) or 0))
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise SheetFormatError(f"unparseable date {value!r}")


def validate_stage(stage: str) -> str:
    stage = (stage or "").strip()
    if stage not in STAGES:
        raise SheetFormatError(f"invalid Stage {stage!r}; expected one of {STAGES}")
    return stage


def parse_rows(values: list[list[Any]], today: date | None = None) -> list[TrackerRow]:
    """Parse a header + data grid (as returned by Sheets) into TrackerRows."""
    if not values:
        raise SheetFormatError("tracker is empty")
    header = [str(h).strip() for h in values[0]]
    if header[: len(COLUMNS)] != COLUMNS:
        raise SheetFormatError(f"unexpected header {header}; expected {COLUMNS}")
    rows = []
    for i, raw in enumerate(values[1:], start=2):
        cells = [str(c) for c in raw] + [""] * (len(COLUMNS) - len(raw))
        if not any(c.strip() for c in cells):
            continue
        rec = dict(zip(COLUMNS, cells))
        rows.append(
            TrackerRow(
                row_number=i,
                connector_id=rec["Connector_ID"].strip(),
                app=rec["App"].strip(),
                contact=rec["Partner_Contact_Name"].strip(),
                email=rec["Partner_Email"].strip().lower(),
                stage=validate_stage(rec["Stage"]),
                last_touched=parse_date(rec["Last_Touched"], today),
                next_action=rec["Next_Action"].strip(),
                notes=rec["Notes"].strip(),
            )
        )
    return rows


def extract_values(data: Any) -> list[list[Any]]:
    """Find the first 'values' grid in a Sheets batch-get response, whatever its nesting."""
    if isinstance(data, dict):
        if isinstance(data.get("values"), list):
            return data["values"]
        for v in data.values():
            found = extract_values(v)
            if found:
                return found
    elif isinstance(data, list):
        for v in data:
            found = extract_values(v)
            if found:
                return found
    return []


def is_stale(row: TrackerRow, today: date, stale_days: int) -> bool:
    if row.stage == "Live" or row.last_touched is None:
        return False
    return (today - row.last_touched).days > stale_days


def stage_counts(rows: list[TrackerRow]) -> dict[str, int]:
    return {s: sum(1 for r in rows if r.stage == s) for s in STAGES}


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------


@dataclass
class Email:
    id: str
    sender: str
    subject: str
    body: str
    date: str = ""

    @property
    def domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower()


def normalize_email(raw: dict[str, Any]) -> Email:
    """Accept fixture shape (id/from/subject/body/date) or Gmail-tool shape."""
    sender = raw.get("from") or raw.get("sender") or ""
    return Email(
        id=str(raw.get("id") or raw.get("messageId") or raw.get("message_id") or ""),
        sender=parseaddr(str(sender))[1].lower(),
        subject=str(raw.get("subject") or ""),
        body=str(raw.get("body") or raw.get("messageText") or raw.get("snippet") or ""),
        date=str(raw.get("date") or raw.get("messageTimestamp") or ""),
    )


def extract_emails(data: Any) -> list[Email]:
    """Find the message list in a Gmail fetch response."""
    if isinstance(data, dict):
        for key in ("messages", "emails"):
            if isinstance(data.get(key), list):
                return [normalize_email(m) for m in data[key] if isinstance(m, dict)]
        for v in data.values():
            found = extract_emails(v)
            if found:
                return found
    return []


@dataclass
class MatchResult:
    row: TrackerRow | None
    method: str  # "email" | "domain" | "app" | "none"
    note: str = ""
    candidates: list[str] = field(default_factory=list)


def match_email(email: Email, rows: list[TrackerRow]) -> MatchResult:
    """Match one email to exactly one row, or return 'needs human'. Never guesses."""
    by_email = [r for r in rows if r.email == email.sender]
    if len(by_email) == 1:
        return MatchResult(by_email[0], "email")
    by_domain = [r for r in rows if r.domain == email.domain]  # exact domain, no suffix match
    if len(by_domain) == 1:
        return MatchResult(by_domain[0], "domain")
    text = f"{email.subject}\n{email.body}".lower()
    by_app = [r for r in rows if r.app and re.search(rf"\b{re.escape(r.app.lower())}\b", text)]
    if len(by_app) == 1 and not by_domain:
        return MatchResult(by_app[0], "app")
    candidates = sorted({r.connector_id for r in by_email + by_domain + by_app})
    return MatchResult(None, "none", NEEDS_HUMAN_AMBIGUOUS, candidates)
