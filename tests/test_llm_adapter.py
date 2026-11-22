"""OpenAI-compatible adapter (OpenRouter, Mistral): the agent loop talks Anthropic-style
messages; the adapter translates both ways. A fake client stands in for the network."""
import json
from datetime import date
from types import SimpleNamespace

from connector_desk.agent import run_agent
from connector_desk.ledger import Ledger
from connector_desk.llm import OpenAICompatLLM
from connector_desk.local_mode import LocalBackend, local_tool_schemas

TODAY = date(2026, 11, 22)


def oai_response(content=None, tool_calls=(), finish="stop"):
    calls = [SimpleNamespace(id=cid, type="function", function=SimpleNamespace(name=name, arguments=args))
             for cid, name, args in tool_calls]
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)])


class FakeClient:
    """Mimics openai.OpenAI(): chat.completions.create(**kw). Replays scripted responses."""

    def __init__(self, *responses):
        self.requests, self._responses = [], list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        return self._responses.pop(0)


TOOL = {"name": "GMAIL_CREATE_EMAIL_DRAFT", "description": "Create a draft.",
        "input_schema": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}}


def test_request_uses_openai_tool_and_system_format():
    client = FakeClient(oai_response("ok"))
    OpenAICompatLLM(client).messages.create(model="m", max_tokens=100, system="SYS", tools=[TOOL],
                                            messages=[{"role": "user", "content": "hi"}])
    req = client.requests[0]
    assert req["model"] == "m"
    assert req["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    assert req["tools"] == [{"type": "function", "function": {
        "name": "GMAIL_CREATE_EMAIL_DRAFT", "description": "Create a draft.", "parameters": TOOL["input_schema"]}}]


def test_tool_calls_come_back_as_tool_use_blocks():
    client = FakeClient(oai_response("thinking", [("call_1", "GMAIL_CREATE_EMAIL_DRAFT", '{"body": "Hi"}')], "tool_calls"))
    resp = OpenAICompatLLM(client).messages.create(model="m", max_tokens=10, system="S", tools=[TOOL],
                                                   messages=[{"role": "user", "content": "hi"}])
    assert [(b.type, getattr(b, "text", None)) for b in resp.content][0] == ("text", "thinking")
    use = resp.content[1]
    assert (use.type, use.id, use.name, use.input) == ("tool_use", "call_1", "GMAIL_CREATE_EMAIL_DRAFT", {"body": "Hi"})


def test_malformed_arguments_become_an_input_the_policy_will_refuse():
    client = FakeClient(oai_response(None, [("c", "GMAIL_CREATE_EMAIL_DRAFT", "{not json")], "tool_calls"))
    resp = OpenAICompatLLM(client).messages.create(model="m", max_tokens=10, system="S", tools=[TOOL],
                                                   messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].input == {"_unparseable_arguments": "{not json"}


def test_history_with_tool_results_is_translated():
    client = FakeClient(oai_response("done"))
    history = [
        {"role": "user", "content": "data"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "c1", "name": "GMAIL_CREATE_EMAIL_DRAFT", "input": {"body": "Hi"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "REFUSED", "is_error": True}]},
    ]
    OpenAICompatLLM(client).messages.create(model="m", max_tokens=10, system="S", tools=[TOOL], messages=history)
    sent = client.requests[0]["messages"][1:]
    assert sent[1] == {"role": "assistant", "content": "ok", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "GMAIL_CREATE_EMAIL_DRAFT", "arguments": json.dumps({"body": "Hi"})}}]}
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: REFUSED"}


def test_full_local_run_through_the_adapter_creates_the_stale_draft(tmp_path):
    body = "Hi Tom, could you share when you expect a decision on our OAuth app? Thanks, Product Ops"
    args = json.dumps({"recipient_email": "tom@garnetbilling.example.com", "subject": "Following up", "body": body})
    client = FakeClient(oai_response(None, [("c1", "GMAIL_CREATE_EMAIL_DRAFT", args)], "tool_calls"),
                        oai_response("Drafted one follow-up."))
    backend = LocalBackend(TODAY)
    report = run_agent(mode="local", llm=OpenAICompatLLM(client), model="m", backend=backend,
                       tools=local_tool_schemas(), ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"),
                       today=TODAY, stale_days=7, gmail_label="l", sheet_id="s", out=lambda s: None)
    assert report.summary["drafts_created"] == ["C007"]
    assert report.final_text == "Drafted one follow-up."
    assert [t["function"]["name"] for t in client.requests[0]["tools"]] == ["GMAIL_CREATE_EMAIL_DRAFT", "GOOGLESHEETS_VALUES_UPDATE"]


def _local(tmp_path, llm):
    return run_agent(mode="local", llm=llm, model="m", backend=LocalBackend(TODAY), tools=local_tool_schemas(),
                     ledger=Ledger(tmp_path / "l.jsonl", "r1", "local"), today=TODAY, stale_days=7,
                     gmail_label="l", sheet_id="s", out=lambda s: None)


def test_provider_errors_and_empty_responses_abort_the_run_cleanly(tmp_path):
    import pytest

    from connector_desk.agent import RunAborted

    class Boom(FakeClient):
        def _create(self, **kw):
            raise ConnectionError("upstream 502")

    for client in (FakeClient(SimpleNamespace(choices=[])), Boom()):
        with pytest.raises(RunAborted, match="model call failed"):
            _local(tmp_path, OpenAICompatLLM(client))


def test_a_reply_cut_off_at_the_token_limit_is_a_warning(tmp_path):
    report = _local(tmp_path, OpenAICompatLLM(FakeClient(oai_response("I updated C00", finish="length"))))
    assert any("cut off" in w for w in report.summary["warnings"])


def test_a_tool_call_without_an_id_gets_one():
    client = FakeClient(oai_response(None, [(None, "GMAIL_CREATE_EMAIL_DRAFT", '{"body": "x"}')], "tool_calls"))
    resp = OpenAICompatLLM(client).messages.create(model="m", max_tokens=10, system="S", tools=[TOOL],
                                                   messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].id
