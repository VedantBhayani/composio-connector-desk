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
    assert set(cfg["tools"]["gmail"]["require_approval"]) >= {"GMAIL_SEND_EMAIL", "GMAIL_SEND_DRAFT"}
