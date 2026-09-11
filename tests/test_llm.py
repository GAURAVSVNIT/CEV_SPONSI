"""The model clients.

Three providers, one message format. The conversions between them are where
tool-calling agents quietly break -- a dropped tool_call_id, two tool results
in separate messages, a temperature a model rejects -- so each one is asserted
against a canned wire payload rather than a live call. Nothing here touches
the network.
"""

from __future__ import annotations

import json

import pytest

from sponsy.config import Config
from sponsy.llm import FakeLLM, LLMError, LLMResponse, ToolCall, build_llm, parse_json
from sponsy.llm.base import task_of
from sponsy.llm.gemini import GeminiLLM, QuotaExhausted, is_daily_quota, retry_delay_of
from sponsy.llm.openai_compat import OpenAILLM

DAILY_429 = ('{"error":{"code":429,"message":"You exceeded your current quota",'
             '"details":[{"violations":[{"quotaId":'
             '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
             '{"retryDelay":"39s"}]}}')
MINUTE_429 = ('{"error":{"code":429,"details":[{"violations":[{"quotaId":'
              '"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},'
              '{"retryDelay":"2s"}]}}')
GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "hi"}]},
                             "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}


class FakeResponse:
    def __init__(self, status, text="", payload=None, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# lenient JSON
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    'here you go:\n```json\n{"a": 1}\n```',
    'prose before {"a": 1} and after',
    '{"a": 1,}',
    '```\n{"a": 1}\n```',
])
def test_json_parsing_is_lenient(raw):
    assert parse_json(raw)["a"] == 1


@pytest.mark.parametrize("raw", ["", "no json here at all", "{broken"])
def test_unparseable_json_raises(raw):
    with pytest.raises(LLMError):
        parse_json(raw)


# ---------------------------------------------------------------------------
# the fake
# ---------------------------------------------------------------------------

def test_the_fake_dispatches_on_the_task_marker(cfg):
    assert task_of("TASK: draft\nrest of it") == "draft"
    assert task_of("no marker") == ""
    out = FakeLLM(cfg).generate(system="TASK: classify\n", messages=[
        {"role": "user", "text": "From: a@b.c\nSubject: Re\n\nplease send the deck"}])
    assert json.loads(out.text)["label"] == "interested"


def test_the_fake_serves_scripted_responses_first(cfg):
    scripted = LLMResponse(text='{"scripted": true}')
    llm = FakeLLM(cfg, scripted=[scripted])
    assert llm.generate(system="TASK: draft", messages=[]) is scripted


def test_the_fake_refuses_a_task_it_has_no_handler_for(cfg):
    with pytest.raises(LLMError, match="no handler"):
        FakeLLM(cfg).generate(system="TASK: nonexistent", messages=[])


def test_build_llm_returns_the_fake_and_loads_the_handlers(cfg):
    from sponsy.llm import FAKE_HANDLERS
    llm = build_llm(cfg)
    assert isinstance(llm, FakeLLM)
    assert {"draft", "research", "classify", "ground", "humanize"} <= set(FAKE_HANDLERS)


def test_ping_works_without_a_task_marker(cfg):
    assert build_llm(cfg).ping() == "OK"


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

@pytest.fixture
def openai_cfg(tmp_path):
    return Config(llm_provider="openai", openai_api_key="k",
                  openai_base_url="https://api.example.com/v1",
                  db_path=tmp_path / "t.db", outbox=tmp_path / "o")


def test_a_tool_round_trip_keeps_the_call_id(openai_cfg):
    """A tool result without its tool_call_id is a 400 from every provider."""
    messages = [
        {"role": "user", "text": "research acme"},
        {"role": "model", "text": "",
         "tool_calls": [ToolCall("fetch_page", {"url": "https://a"}, id="call_1")]},
        {"role": "tool", "name": "fetch_page", "id": "call_1",
         "response": {"title": "A"}},
    ]
    out = OpenAILLM._to_messages("SYSTEM", messages)
    assert out[0] == {"role": "system", "content": "SYSTEM"}
    assert out[2]["tool_calls"][0]["id"] == "call_1"
    assert out[2]["tool_calls"][0]["function"]["name"] == "fetch_page"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == \
        {"url": "https://a"}
    assert out[3] == {"role": "tool", "tool_call_id": "call_1",
                      "content": '{"title": "A"}'}


def test_a_missing_call_id_is_synthesised(openai_cfg):
    messages = [{"role": "model", "text": "",
                 "tool_calls": [ToolCall("web_search", {"query": "x"})]}]
    assert OpenAILLM._to_messages("", messages)[0]["tool_calls"][0]["id"]


def test_parsing_a_chat_completion_with_tool_calls(openai_cfg):
    payload = {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": None,
        "tool_calls": [{"id": "call_9", "type": "function", "function": {
            "name": "web_search", "arguments": '{"query": "acme students"}'}}]}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    out = OpenAILLM(openai_cfg)._parse(payload, "gpt-5-mini")
    assert out.tool_calls[0].name == "web_search"
    assert out.tool_calls[0].args == {"query": "acme students"}
    assert out.tool_calls[0].id == "call_9"
    assert out.prompt_tokens == 1000 and out.output_tokens == 100
    assert out.cost_usd == pytest.approx(1000 * 0.25 / 1e6 + 100 * 2.00 / 1e6)


def test_malformed_tool_arguments_do_not_crash_the_loop(openai_cfg):
    payload = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c", "function": {"name": "f", "arguments": "not json"}}]}}]}
    assert OpenAILLM(openai_cfg)._parse(payload, "gpt-5-mini").tool_calls[0].args == {}


def test_content_returned_as_parts_is_joined(openai_cfg):
    payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
    assert OpenAILLM(openai_cfg)._parse(payload, "gpt-5-mini").text == "ab"


def test_no_choices_is_an_error(openai_cfg):
    with pytest.raises(LLMError):
        OpenAILLM(openai_cfg)._parse({"choices": []}, "gpt-5-mini")


def _capture(monkeypatch, payload=None, status=200):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"], sent["body"], sent["headers"] = url, json, headers
        return FakeResponse(status, payload=payload or {"choices": [
            {"message": {"content": "ok"}}]})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    return sent


def test_temperature_is_omitted_for_models_that_reject_it(openai_cfg, monkeypatch):
    sent = _capture(monkeypatch)
    OpenAILLM(openai_cfg, model="gpt-5-mini").generate(
        system="s", messages=[{"role": "user", "text": "hi"}], temperature=0.7)
    assert "temperature" not in sent["body"]


def test_temperature_is_sent_to_models_that_take_it(openai_cfg, monkeypatch):
    sent = _capture(monkeypatch)
    OpenAILLM(openai_cfg, model="llama-3.3-70b").generate(
        system="s", messages=[{"role": "user", "text": "hi"}], temperature=0.7)
    assert sent["body"]["temperature"] == 0.7


def test_tools_are_sent_in_the_function_shape(openai_cfg, monkeypatch):
    sent = _capture(monkeypatch)
    OpenAILLM(openai_cfg).generate(
        system="s", messages=[{"role": "user", "text": "hi"}],
        tools=[{"name": "web_search", "description": "search",
                "parameters": {"type": "object", "properties": {}}}])
    tool = sent["body"]["tools"][0]
    assert tool["type"] == "function" and tool["function"]["name"] == "web_search"
    assert sent["body"]["tool_choice"] == "auto"


def test_json_mode_is_asked_for_only_when_there_are_no_tools(openai_cfg, monkeypatch):
    sent = _capture(monkeypatch)
    OpenAILLM(openai_cfg).generate(system="s", messages=[{"role": "user", "text": "x"}],
                                   response_json=True)
    assert sent["body"]["response_format"] == {"type": "json_object"}


def test_the_base_url_is_honoured(openai_cfg, monkeypatch):
    sent = _capture(monkeypatch)
    OpenAILLM(openai_cfg).generate(system="", messages=[{"role": "user", "text": "x"}])
    assert sent["url"] == "https://api.example.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer k"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def test_tool_results_for_one_turn_go_in_a_single_user_message(tmp_path):
    """Splitting them across messages quietly teaches the model to stop making
    parallel calls."""
    from sponsy.llm.anthropic_ import AnthropicLLM

    messages = [
        {"role": "user", "text": "go"},
        {"role": "model", "text": "",
         "tool_calls": [ToolCall("a", {}, id="t1"), ToolCall("b", {}, id="t2")]},
        {"role": "tool", "name": "a", "id": "t1", "response": {"ok": 1}},
        {"role": "tool", "name": "b", "id": "t2", "response": {"error": "boom"}},
    ]
    out = AnthropicLLM._to_messages(messages)
    assert len(out) == 3
    assert out[2]["role"] == "user" and len(out[2]["content"]) == 2
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]
    assert out[2]["content"][1]["is_error"] is True


def test_a_model_turn_is_replayed_verbatim_when_we_have_the_parts(tmp_path):
    """Thinking blocks have to go back unchanged on the same model."""
    from sponsy.llm.anthropic_ import AnthropicLLM

    parts = [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "hi"}]
    out = AnthropicLLM._to_messages([{"role": "model", "text": "hi", "parts": parts}])
    assert out[0] == {"role": "assistant", "content": parts}


# ---------------------------------------------------------------------------
# Gemini: key rotation is what keeps a free tier usable
# ---------------------------------------------------------------------------

@pytest.fixture
def two_keys(tmp_path):
    GeminiLLM._exhausted.clear()
    GeminiLLM._next_slot.clear()
    cfg = Config(llm_provider="gemini", gemini_api_key="KEY-AAAA",
                 gemini_api_keys=["KEY-BBBB"], min_interval_s=0,
                 db_path=tmp_path / "t.db", outbox=tmp_path / "o")
    yield cfg
    GeminiLLM._exhausted.clear()
    GeminiLLM._next_slot.clear()


def test_both_keys_are_loaded_in_order(two_keys):
    assert two_keys.api_keys == ["KEY-AAAA", "KEY-BBBB"]


def test_duplicate_keys_are_collapsed(tmp_path):
    cfg = Config(gemini_api_key="SAME", gemini_api_keys=["SAME", "OTHER"],
                 db_path=tmp_path / "t.db", outbox=tmp_path / "o")
    assert cfg.api_keys == ["SAME", "OTHER"]


def test_a_daily_cap_rotates_to_the_next_key(two_keys, monkeypatch):
    used: list[str] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        key = headers["x-goog-api-key"]
        used.append(key)
        if key == "KEY-AAAA":
            return FakeResponse(429, DAILY_429)
        return FakeResponse(200, payload=GEMINI_OK)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = GeminiLLM(two_keys).generate(system="s",
                                       messages=[{"role": "user", "text": "hi"}])
    assert out.text == "hi"
    assert used == ["KEY-AAAA", "KEY-BBBB"]


def test_when_every_key_is_dry_it_says_so_instead_of_retrying(two_keys, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: FakeResponse(429, DAILY_429))
    with pytest.raises(QuotaExhausted):
        GeminiLLM(two_keys).generate(system="s",
                                     messages=[{"role": "user", "text": "hi"}])


def test_an_exhausted_key_is_remembered_across_processes(two_keys, monkeypatch,
                                                         tmp_path):
    from sponsy.state import Database

    db = Database(tmp_path / "mem.db")
    llm = GeminiLLM(two_keys, db=db)
    llm.retire_key("KEY-AAAA")
    GeminiLLM._exhausted.clear()
    assert GeminiLLM(two_keys, db=db).current_key() == "KEY-BBBB"
    db.close()


def test_a_per_minute_limit_is_waited_out_not_treated_as_a_daily_cap():
    assert is_daily_quota(DAILY_429)
    assert not is_daily_quota(MINUTE_429)
    assert retry_delay_of(MINUTE_429) == 2.0
    assert retry_delay_of("no retry info") is None


def test_a_client_error_is_not_retried(two_keys, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append(1)
        return FakeResponse(400, "bad request")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(LLMError, match="400"):
        GeminiLLM(two_keys).generate(system="s",
                                     messages=[{"role": "user", "text": "hi"}])
    assert len(calls) == 1


def test_a_tool_call_keeps_its_thought_signature(two_keys):
    """Gemini 3.x rejects the next request if the signature is not echoed."""
    payload = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "fetch_page", "args": {"url": "https://a"}},
         "thoughtSignature": "sig-123"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}}
    out = GeminiLLM(two_keys)._parse(payload, "gemini-3.5-flash")
    assert out.tool_calls[0].thought_signature == "sig-123"
    replayed = GeminiLLM._to_contents([{"role": "model", "text": "",
                                        "tool_calls": out.tool_calls}])
    assert replayed[0]["parts"][0]["thoughtSignature"] == "sig-123"


def test_thinking_parts_are_not_read_as_text(two_keys):
    payload = {"candidates": [{"content": {"parts": [
        {"text": "internal", "thought": True}, {"text": "answer"}]}}],
        "usageMetadata": {}}
    assert GeminiLLM(two_keys)._parse(payload, "gemini-3.5-flash").text == "answer"


def test_a_blocked_response_is_an_error_not_an_empty_string(two_keys):
    with pytest.raises(LLMError, match="no candidates"):
        GeminiLLM(two_keys)._parse({"candidates": []}, "gemini-3.5-flash")


def test_tool_results_are_sent_as_function_responses(two_keys):
    out = GeminiLLM._to_contents([{"role": "tool", "name": "fetch_page",
                                   "response": {"title": "A"}}])
    assert out[0]["role"] == "user"
    assert out[0]["parts"][0]["functionResponse"]["name"] == "fetch_page"


INVALID_ARG = ('{"error":{"code":400,"message":"Request contains an invalid '
               'argument.","status":"INVALID_ARGUMENT"}}')


def test_a_model_that_refuses_thinkingBudget_zero_is_retried_without_it(two_keys,
                                                                       monkeypatch):
    """gemini-3.5-flash-lite rejects thinkingBudget:0 with a bare "invalid
    argument" while the main model accepts it. LLM_THINKING_BUDGET=0 is the
    documented cheapest setting, so this broke every cheap-model call --
    grounding, reply classification, sponsor extraction -- and the 400 said
    nothing about thinking.
    """
    sent: list[dict] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        import copy
        # the caller mutates generationConfig in place on the retry, so this
        # has to be a snapshot or every recorded body looks like the last one
        sent.append(copy.deepcopy(json))
        if "thinkingConfig" in json["generationConfig"]:
            return FakeResponse(400, INVALID_ARG)
        return FakeResponse(200, payload=GEMINI_OK)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    llm = GeminiLLM(two_keys)
    llm.generate(system="s", messages=[{"role": "user", "text": "hi"}])

    assert len(sent) == 2
    assert "thinkingConfig" in sent[0]["generationConfig"]
    assert "thinkingConfig" not in sent[1]["generationConfig"]


def test_the_refusal_is_remembered_so_it_is_not_paid_for_twice(two_keys, monkeypatch):
    """One wasted round trip to learn it is fine; one per call is not."""
    sent: list[dict] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        import copy
        # the caller mutates generationConfig in place on the retry, so this
        # has to be a snapshot or every recorded body looks like the last one
        sent.append(copy.deepcopy(json))
        if "thinkingConfig" in json["generationConfig"]:
            return FakeResponse(400, INVALID_ARG)
        return FakeResponse(200, payload=GEMINI_OK)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    llm = GeminiLLM(two_keys)
    for _ in range(3):
        llm.generate(system="s", messages=[{"role": "user", "text": "hi"}])

    assert len(sent) == 4          # one failed probe, then three clean calls
    assert not any("thinkingConfig" in s["generationConfig"] for s in sent[1:])


def test_a_plain_400_is_still_not_retried(two_keys, monkeypatch):
    """The retry is for one specific refusal, not a licence to retry every
    client error -- a malformed request must still fail loudly and at once."""
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append(1)
        return FakeResponse(400, "bad request")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(LLMError, match="400"):
        GeminiLLM(two_keys).generate(system="s",
                                     messages=[{"role": "user", "text": "hi"}])
    assert len(calls) == 1
