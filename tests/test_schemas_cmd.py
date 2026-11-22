"""`connector_desk schemas`: confirm the real tool slugs/params before the first dry run.

Needs only COMPOSIO_API_KEY (no Google connection). The Composio SDK is faked at its boundary.
"""
import json
import sys
from types import SimpleNamespace

from connector_desk.cli import main


def tool(slug, required=(), props=(), outputs=(), deprecated=False):
    return SimpleNamespace(
        slug=slug, is_deprecated=deprecated,
        input_parameters={"type": "object", "required": list(required), "properties": {p: {"type": "string"} for p in props}},
        output_parameters={"type": "object", "properties": {p: {} for p in outputs}},
    )


KNOWN = {
    "GMAIL_FETCH_EMAILS": tool("GMAIL_FETCH_EMAILS", props=["query", "max_results", "verbose"], outputs=["messages"]),
    "GOOGLESHEETS_VALUES_UPDATE": tool("GOOGLESHEETS_VALUES_UPDATE", ["spreadsheet_id", "range", "value_input_option", "values"],
                                       ["spreadsheet_id", "range", "value_input_option", "values"], deprecated=True),
}


class FakeComposio:
    def __init__(self, api_key=None, **_):
        assert api_key == "ck"

        def by_slug(slug):
            if slug not in KNOWN:
                raise LookupError(f"tool {slug} not found")
            return KNOWN[slug]

        self.tools = SimpleNamespace(get_raw_composio_tool_by_slug=by_slug)


def test_schemas_prints_deprecation_and_params_and_saves_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "composio", SimpleNamespace(Composio=FakeComposio))
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck")
    out_file = tmp_path / "schemas.json"
    assert main(["schemas", "--out", str(out_file)]) == 0
    out = capsys.readouterr().out
    assert "GOOGLESHEETS_VALUES_UPDATE DEPRECATED" in out
    assert "required: spreadsheet_id, range, value_input_option, values" in out
    assert "GMAIL_SEND_EMAIL: ERROR" in out and "not found" in out  # blocked tools are checked too
    saved = json.loads(out_file.read_text())
    assert saved["GMAIL_FETCH_EMAILS"]["inputs"] == ["query", "max_results", "verbose"]
    assert saved["GMAIL_FETCH_EMAILS"]["outputs"] == ["messages"]


def test_schemas_needs_only_the_composio_key(monkeypatch, capsys):
    monkeypatch.setenv("COMPOSIO_API_KEY", "")
    assert main(["schemas"]) == 2
    assert "COMPOSIO_API_KEY" in capsys.readouterr().err


def test_a_missing_deprecation_flag_is_reported_as_unknown(tmp_path, monkeypatch, capsys):
    bare = SimpleNamespace(slug="GMAIL_FETCH_EMAILS", input_parameters={}, output_parameters={})

    class NoFlag(FakeComposio):
        def __init__(self, api_key=None, **_):
            self.tools = SimpleNamespace(get_raw_composio_tool_by_slug=lambda slug: bare)

    monkeypatch.setitem(sys.modules, "composio", SimpleNamespace(Composio=NoFlag))
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck")
    assert main(["schemas", "--out", str(tmp_path / "s.json")]) == 0
    assert "deprecated: unknown" in capsys.readouterr().out
