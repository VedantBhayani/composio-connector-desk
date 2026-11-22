from datetime import date

import pytest

from connector_desk.local_mode import load_email_fixtures, load_tracker_grid
from connector_desk.sheet_schema import (
    NEEDS_HUMAN_AMBIGUOUS, SheetFormatError, extract_values, is_stale, match_email,
    normalize_email, parse_rows, stage_counts, validate_stage,
)

TODAY = date(2026, 11, 22)


@pytest.fixture
def rows():
    return parse_rows(load_tracker_grid(TODAY), TODAY)


def emails_by_id():
    return {e["id"]: normalize_email(e) for e in load_email_fixtures(TODAY)}


def test_fixture_has_ten_fictional_rows(rows):
    assert [r.connector_id for r in rows] == [f"C{i:03d}" for i in range(1, 11)]
    assert all(r.email.endswith(".example.com") for r in rows)
    assert rows[0].row_number == 2


def test_stage_enum_validation():
    for s in ["Requested", "OAuth app submitted", "Sandbox credentials", "Partner verification", "Live"]:
        assert validate_stage(s) == s
    with pytest.raises(SheetFormatError):
        validate_stage("Done")
    grid = load_tracker_grid(TODAY)
    grid[1][4] = "Shipped"
    with pytest.raises(SheetFormatError):
        parse_rows(grid, TODAY)


def test_bad_header_rejected():
    with pytest.raises(SheetFormatError):
        parse_rows([["ID", "App"], ["C1", "x"]], TODAY)


def test_stale_detection_with_fixed_today(rows):
    stale = {r.connector_id for r in rows if is_stale(r, TODAY, 7)}
    # 12d, 9d, 15d, 8d, 20d stale; C005 is 30d but Live; 7 is not "older than 7"
    assert stale == {"C001", "C002", "C004", "C007", "C008"}
    assert {r.connector_id for r in rows if is_stale(r, TODAY, 10)} == {"C001", "C004", "C008"}


def test_stage_counts(rows):
    c = stage_counts(rows)
    assert c == {"Requested": 3, "OAuth app submitted": 2, "Sandbox credentials": 2, "Partner verification": 2, "Live": 1}


@pytest.mark.parametrize(
    "email_id,expected",
    [("fixture-01", "C001"), ("fixture-02", "C002"), ("fixture-03", "C003"), ("fixture-04", "C004"), ("fixture-06", "C008")],
)
def test_matches(rows, email_id, expected):
    assert match_email(emails_by_id()[email_id], rows).row.connector_id == expected


def test_ambiguous_match_returns_needs_human(rows):
    res = match_email(emails_by_id()["fixture-05"], rows)
    assert res.row is None
    assert res.note == NEEDS_HUMAN_AMBIGUOUS


def test_two_apps_named_is_ambiguous(rows):
    e = normalize_email({"id": "x", "from": "someone@other.example.com", "subject": "Acme CRM and Iris Notes", "body": ""})
    assert match_email(e, rows).note == NEEDS_HUMAN_AMBIGUOUS


def test_extract_values_from_nested_response():
    data = {"data": {"valueRanges": [{"range": "Tracker!A1:B2", "values": [["a"], ["b"]]}]}}
    assert extract_values(data) == [["a"], ["b"]]


def test_normalize_gmail_shape():
    e = normalize_email({"messageId": "m1", "sender": "Dana Lee <DANA@acmecrm.example.com>", "subject": "s", "messageText": "b"})
    assert (e.id, e.sender, e.body) == ("m1", "dana@acmecrm.example.com", "b")


def test_self_sent_seed_emails_still_match_by_app_name(rows):
    """Seeded by hand, every email's real sender is the throwaway inbox itself."""
    expected = {"fixture-01": "C001", "fixture-02": "C002", "fixture-03": "C003", "fixture-04": "C004",
                "fixture-05": None, "fixture-06": "C008"}
    for e in load_email_fixtures(TODAY):
        e["from"] = "Throwaway <me@throwaway-inbox.example.org>"
        res = match_email(normalize_email(e), rows)
        assert (res.row.connector_id if res.row else None) == expected[e["id"]], e["id"]
