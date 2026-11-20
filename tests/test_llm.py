import json
import subprocess
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from syncret.core import llm
from syncret.core.llm import AnthropicApi, ClaudeCode, Fake, Llm, LlmError, ProviderError, matches

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["same", "different"]}},
    "required": ["label"],
    "additionalProperties": False
}


# a provider that replays a list of answers or errors and records each call
class Scripted:
    def __init__(self, name, steps, can_see_own=False):
        self.name, self.steps, self.can_see_own, self.calls = name, list(steps), can_see_own, 0

    def ask(self, job, system, content, schema):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


# an Llm over scripted providers that records backoff sleeps instead of sleeping
def _llm(*providers, jobs=None):
    sleeps = []
    chain = Llm({p.name: p for p in providers}, jobs or {}, sleep=sleeps.append)
    return chain, sleeps


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# config: none means no llm; providers and jobs load in order
def test_load(tmp_path):
    assert Llm.load(tmp_path / "missing.toml") is None
    path = _write(tmp_path, """
[providers.claude]
type = "claude-code"
can_see_own = true

[providers.opus]
type = "anthropic"
model = "claude-opus-5-5"

[jobs]
merge = ["opus", "claude"]
""")
    chain = Llm.load(path)
    assert chain is not None
    assert list(chain.providers) == ["claude", "opus"]
    assert [p.name for p in chain.chain("merge", own=False)] == ["opus", "claude"]
    assert [p.name for p in chain.chain("review", own=False)] == ["claude", "opus"]


# the config path follows XDG_CONFIG_HOME
def test_default_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert llm.default_config_path() == tmp_path / "syncret" / "config.toml"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[providers.x]\ntype = "nope"\n', "unknown type 'nope'"),
        ('[providers.x]\ntype = "fake"\ncolour = "red"\n', "provider 'x'"),
        ('[providers.x]\ntype = "fake"\n[jobs]\nreview = ["y"]\n', "unknown providers \\['y'\\]"),
        ('[providers.x]\ntype = "fake"\n[jobs]\nsing = ["x"]\n', "unknown job 'sing'"),
        ("[providers\n", "config.toml")
    ]
)
def test_load_errors(tmp_path, text, message):
    with pytest.raises(LlmError, match=message):
        Llm.load(_write(tmp_path, text))


# keys are never read from the config, only from the env var it names
def test_api_key_env(monkeypatch):
    monkeypatch.delenv("SYNCRET_TEST_KEY", raising=False)
    with pytest.raises(ProviderError, match="SYNCRET_TEST_KEY is not set"):
        AnthropicApi("opus", api_key_env="SYNCRET_TEST_KEY").ask("review", "s", "c", SCHEMA)


# a rate limit is retried with backoff, then the chain falls back to the next provider
def test_fallback_after_retries():
    first = Scripted("first", [ProviderError("429", retry=True)] * 3)
    second = Scripted("second", [{"label": "same"}])
    chain, sleeps = _llm(first, second)
    answer = chain.ask("review", "s", "c", SCHEMA, own=False)
    assert answer.provider == "second"
    assert answer.data == {"label": "same"}
    assert first.calls == 3
    assert sleeps == [1, 2]


# a failure that will not go away moves on at once
def test_no_retry_on_hard_failure():
    first = Scripted("first", [ProviderError("400 bad request")])
    second = Scripted("second", [{"label": "different"}])
    chain, sleeps = _llm(first, second)
    assert chain.ask("review", "s", "c", SCHEMA, own=False).provider == "second"
    assert first.calls == 1
    assert sleeps == []


# when every provider fails, the error names each of them
def test_all_fail():
    chain, _ = _llm(Scripted("a", [ProviderError("down")]), Scripted("b", [ProviderError("off")]))
    with pytest.raises(LlmError, match="a: down; b: off"):
        chain.ask("review", "s", "c", SCHEMA, own=False)


# an answer that breaks the schema counts as a failure
def test_schema_checked():
    first = Scripted("first", [{"label": "maybe"}] * 3)
    second = Scripted("second", [{"label": "same"}])
    chain, _ = _llm(first, second)
    assert chain.ask("review", "s", "c", SCHEMA, own=False).provider == "second"


# own skills never reach a provider whose can_see_own switch is off
def test_own_gate():
    public = Scripted("public", [{"label": "same"}])
    private = Scripted("private", [{"label": "different"}], can_see_own=True)
    chain, _ = _llm(public, private)
    assert chain.ask("review", "s", "c", SCHEMA, own=True).provider == "private"
    assert public.calls == 0
    chain, _ = _llm(Scripted("public", [{"label": "same"}]))
    with pytest.raises(LlmError, match="can_see_own"):
        chain.ask("review", "s", "c", SCHEMA, own=True)


# the own gate also holds for providers loaded from config, which default to off
def test_own_gate_default_off(tmp_path):
    chain = Llm.load(_write(tmp_path, '[providers.x]\ntype = "fake"\n'))
    assert chain is not None
    assert chain.chain("review", own=True) == []


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ({"label": "same"}, True),
        ({"label": "maybe"}, False),
        ({}, False),
        ({"label": "same", "extra": 1}, False),
        ("same", False)
    ]
)
def test_matches(value, ok):
    assert matches(value, SCHEMA) is ok


# arrays, numbers and booleans in nested schemas
def test_matches_nested():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "integer"}},
        "flag": {"type": "boolean"}
    }}
    assert matches({"items": [1, 2], "flag": True}, schema)
    assert not matches({"items": [1, True]}, schema)
    assert not matches({"flag": 1}, schema)


# the fake provider answers from its file and records what it was sent
def test_fake(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"review": {"label": "same"}}))
    fake = Fake("f", answers=str(answers))
    assert fake.ask("review", "s", "content", SCHEMA) == {"label": "same"}
    assert fake.calls == [("review", "content")]
    with pytest.raises(ProviderError, match="no fake answer for merge"):
        fake.ask("merge", "s", "c", SCHEMA)


# claude code: no tools, no mcp, no session, schema enforced, content on stdin
def test_claude_code_argv(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["input"] = argv, kwargs["input"]
        reply = {"type": "result", "is_error": False, "structured_output": {"label": "same"}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "run", run)
    data = ClaudeCode("cc", model="opus").ask("merge", "be careful", "skill text", SCHEMA)
    assert data == {"label": "same"}
    argv = seen["argv"]
    assert argv[:2] == ["/bin/claude", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--model") + 1] == "opus"
    assert seen["input"] == "skill text"
    assert "skill text" not in argv


@pytest.mark.parametrize(
    ("stdout", "code", "data", "error"),
    [
        (json.dumps({"is_error": False, "result": '{"label": "different"}'}), 0,
         {"label": "different"}, None),
        (json.dumps({"is_error": True, "result": "usage limit reached"}), 1, None,
         "usage limit reached"),
        ("not json", 1, None, "exit 1"),
        (json.dumps({"is_error": False, "result": "sure!"}), 0, None, "not json")
    ]
)
def test_claude_code_replies(monkeypatch, stdout, code, data, error):
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, code, stdout, ""))
    provider = ClaudeCode("cc")
    if error:
        with pytest.raises(ProviderError, match=error):
            provider.ask("review", "s", "c", SCHEMA)
    else:
        assert provider.ask("review", "s", "c", SCHEMA) == data


# claude code missing or slow
def test_claude_code_unavailable(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="not installed"):
        ClaudeCode("cc").ask("review", "s", "c", SCHEMA)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "run", slow)
    with pytest.raises(ProviderError) as info:
        ClaudeCode("cc").ask("review", "s", "c", SCHEMA)
    assert info.value.retry


# a stand-in for client.beta.messages.stream that records its arguments
class FakeClient:
    def __init__(self, outcome):
        self.outcome, self.kwargs = outcome, {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        self.kwargs = kwargs
        outcome = self.outcome

        class Stream:
            def __enter__(self):
                if isinstance(outcome, Exception):
                    raise outcome
                return SimpleNamespace(get_final_message=lambda: outcome)

            def __exit__(self, *exc):
                return False

        return Stream()


# a final message with one text block
def _message(text, stop="end_turn", category=None):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category),
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)]
    )


# an sdk error built the way the sdk builds it
def _api_error(cls, status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.example.test"))
    return cls("boom", response=response, body=None)


# anthropic: model, schema, effort and refusal fallbacks are sent; the json answer is parsed
def test_anthropic_request():
    client = FakeClient(_message('{"label": "same"}'))
    provider = AnthropicApi("opus", client=client)
    assert provider.ask("review", "be careful", "skill text", SCHEMA) == {"label": "same"}
    kwargs = client.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["system"] == "be careful"
    assert kwargs["messages"] == [{"role": "user", "content": "skill text"}]
    assert kwargs["output_config"] == {
        "effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}
    }
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert "temperature" not in kwargs


# fallbacks can be turned off in config
def test_anthropic_fallbacks_off():
    client = FakeClient(_message('{"label": "same"}'))
    AnthropicApi("opus", fallbacks=False, client=client).ask("merge", "s", "c", SCHEMA)
    assert "fallbacks" not in client.kwargs
    assert client.kwargs["output_config"]["effort"] == "high"


@pytest.mark.parametrize(
    ("outcome", "message", "retry"),
    [
        (_message("", stop="refusal", category="cyber"), "refused \\(cyber\\)", False),
        (_message('{"label": "sa', stop="max_tokens"), "max_tokens", False),
        (_message("sure!"), "not json", True),
        (_api_error(anthropic.RateLimitError, 429), "RateLimitError", True),
        (_api_error(anthropic.InternalServerError, 529), "529", True),
        (_api_error(anthropic.BadRequestError, 400), "400", False),
        (_api_error(anthropic.AuthenticationError, 401), "401", False)
    ]
)
def test_anthropic_failures(outcome, message, retry):
    with pytest.raises(ProviderError, match=message) as info:
        AnthropicApi("opus", client=FakeClient(outcome)).ask("review", "s", "c", SCHEMA)
    assert info.value.retry is retry


# llm check: each provider gets the fixed question, never skill content; exit reflects results
def test_cli_llm_check(tmp_path, monkeypatch, capsys):
    from syncret.cli import main

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"check": {"ok": True}}))
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    path = _write(tmp_path, f"""
[providers.fake]
type = "fake"
answers = "{answers}"
can_see_own = true

[providers.claude]
type = "claude-code"
""")
    assert main(["llm", "check", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "ok    fake  fake, default model, may see own skills" in out
    assert "fail  claude  claude-code, default model, upstream skills only" in out
    assert "claude is not installed" in out


# llm check: none working exits 1, a broken or missing config exits 2
def test_cli_llm_check_exits(tmp_path, monkeypatch, capsys):
    from syncret.cli import main

    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    claude_only = _write(tmp_path, '[providers.c]\ntype = "claude-code"\n')
    assert main(["llm", "check", "--config", str(claude_only)]) == 1
    assert main(["llm", "check", "--config", str(tmp_path / "missing.toml")]) == 2
    broken = _write(tmp_path, '[providers.x]\ntype = "nope"\n')
    assert main(["llm", "check", "--config", str(broken)]) == 2
    assert "unknown type" in capsys.readouterr().err
