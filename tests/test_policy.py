import pytest

from connector_desk import tools_policy as tp


@pytest.mark.parametrize("tool", sorted(tp.ALLOWED_TOOLS))
def test_allowed_tools_pass(tool):
    assert tp.check_tool(tool)


@pytest.mark.parametrize("tool", sorted(tp.BLOCKED_TOOLS))
def test_blocked_tools_raise(tool):
    with pytest.raises(tp.ToolBlockedError, match="BLOCKED"):
        tp.check_tool(tool)


@pytest.mark.parametrize(
    "tool", ["GMAIL_MOVE_TO_TRASH", "GOOGLESHEETS_DELETE_SHEET", "GITHUB_CREATE_ISSUE", "", "gmail_fetch_emails"]
)
def test_unknown_tools_denied(tool):
    with pytest.raises(tp.ToolNotAllowedError, match="DENIED"):
        tp.check_tool(tool)


def test_allow_list_has_no_dangerous_tools():
    assert not [t for t in tp.ALLOWED_TOOLS if tp.DANGEROUS_NAME.search(t)]
    assert not set(tp.ALLOWED_TOOLS) & tp.BLOCKED_TOOLS


def test_every_allowed_tool_has_a_reason():
    assert all(reason.strip() for reason in tp.ALLOWED_TOOLS.values())


def test_session_config_mirrors_allow_list():
    cfg = tp.build_session_config("u1")
    assert cfg["toolkits"] == ["gmail", "googlesheets"]
    assert set(cfg["tools"]) == {"gmail", "googlesheets"}
    enabled = {t for kit in cfg["tools"].values() for t in kit["enable"]}
    assert enabled == set(tp.ALLOWED_TOOLS)
    assert cfg["session_preset"] == "direct_tools"
    assert cfg["sandbox"] == {"enable": False}
    # Composio rejects a session that names any tool outside the enabled set (live 400:
    # "require_approval lists GMAIL_FORWARD_MESSAGE, which this session's tools filter excludes").
    named = {t for kit in cfg["tools"].values() for key, ts in kit.items() for t in ts}
    assert named == enabled


def test_session_config_binds_to_installed_sdk_signature():
    """Catches renamed SDK parameters offline. Does not prove the server accepts the config."""
    import inspect

    tr = pytest.importorskip("composio.core.models.tool_router")
    sig = inspect.signature(tr.ToolRouter.create)
    sig.bind(None, **tp.build_session_config("u1"))  # raises TypeError on unknown kwargs
    from composio import SESSION_PRESET_DIRECT_TOOLS
    assert tp.build_session_config("u1")["session_preset"] == SESSION_PRESET_DIRECT_TOOLS


def test_no_legacy_composio_core_imported():
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1]
    for p in list((root / "src").rglob("*.py")) + [root / "requirements.txt"]:
        assert not re.search(r"composio[-_]core|from composio\.core\b.*import Composio", p.read_text()), p
