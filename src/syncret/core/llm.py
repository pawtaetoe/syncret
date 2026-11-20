import json
import os
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JOBS = ("review", "overlap", "merge")
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False
}
# effort per job: first-pass checks stay cheap, merges get more thought
EFFORT = {"review": "medium", "overlap": "medium", "merge": "high", "check": "low"}


# where the config lives unless --config says otherwise
def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "syncret" / "config.toml"


# the whole chain failed, or the config is wrong
class LlmError(Exception):
    pass


# one provider failed; retry marks failures worth trying again on the same provider
class ProviderError(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


# one answer and the provider that gave it
@dataclass(frozen=True)
class Answer:
    provider: str
    data: dict


# true when value matches the subset of json schema syncret's own schemas use
def matches(value, schema):
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool)
    }
    if kind and not checks[kind](value):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(matches(value[k], s) for k, s in properties.items() if k in value)
    if kind == "array":
        return all(matches(item, schema.get("items", {})) for item in value)
    return True


# claude code headless: no tools, no mcp, no saved session, so a skill under review cannot act
@dataclass
class ClaudeCode:
    name: str
    can_see_own: bool = False
    model: str = ""
    command: str = "claude"
    timeout: int = 600
    token_file: str = ""

    # the environment for claude: a long-lived token from token_file when one is set
    def _env(self):
        if not self.token_file:
            return None
        path = Path(self.token_file).expanduser()
        if not path.is_file():
            raise ProviderError(f"token file {path} does not exist")
        token = path.read_text().strip().removeprefix("CLAUDE_CODE_OAUTH_TOKEN=").strip()
        return {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}

    def ask(self, job, system, content, schema):
        binary = shutil.which(self.command)
        if not binary:
            raise ProviderError(f"{self.command} is not installed or not on PATH")
        argv = [binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--system-prompt", system, "--effort", EFFORT.get(job, "medium")]
        if self.model:
            argv += ["--model", self.model]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, content goes on stdin
                argv, input=content, capture_output=True, text=True, timeout=self.timeout,
                check=False, env=self._env()
            )
        except subprocess.TimeoutExpired as error:
            raise ProviderError(f"timed out after {self.timeout}s", retry=True) from error
        try:
            reply = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            lines = (result.stderr or result.stdout).strip().splitlines() or ["no output"]
            raise ProviderError(f"exit {result.returncode}: {lines[-1]}") from error
        if result.returncode or reply.get("is_error"):
            raise ProviderError(str(reply.get("result") or reply.get("subtype") or "failed"))
        data = reply.get("structured_output")
        if data is None:
            try:
                data = json.loads(reply.get("result", ""))
            except json.JSONDecodeError as error:
                raise ProviderError("answer is not json", retry=True) from error
        return data


# anthropic api through the official sdk; refusal fallbacks on unless turned off
@dataclass
class AnthropicApi:
    name: str
    can_see_own: bool = False
    model: str = "claude-opus-5-5"
    api_key_env: str = ""
    fallbacks: bool = True
    max_tokens: int = 64000
    client: Any = None

    def _client(self):
        if self.client is None:
            try:
                import anthropic
            except ImportError as error:
                raise ProviderError("install syncret[anthropic] to use this provider") from error
            key = None
            if self.api_key_env:
                key = os.environ.get(self.api_key_env)
                if not key:
                    raise ProviderError(f"{self.api_key_env} is not set")
            self.client = anthropic.Anthropic(api_key=key, max_retries=0)
        return self.client

    def ask(self, job, system, content, schema):
        # optional extra, imported on use
        import anthropic

        client = self._client()
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            with client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": EFFORT.get(job, "medium"),
                    "format": {"type": "json_schema", "schema": schema}
                },
                **(extra if self.fallbacks else {})
            ) as stream:
                message = stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as error:
            raise ProviderError(type(error).__name__, retry=True) from error
        except anthropic.APIStatusError as error:
            raise ProviderError(f"{error.status_code}: {error.message}",
                                retry=error.status_code >= 500) from error
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise ProviderError(f"refused ({getattr(details, 'category', None) or 'no category'})")
        if message.stop_reason == "max_tokens":
            raise ProviderError("answer cut off at max_tokens")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderError("answer is not json", retry=True) from error


# hosts that may be reached over plain http, since the traffic never leaves the machine
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


# any server speaking the openai chat completions api: openrouter, ollama, meta's model api
@dataclass
class OpenAiCompatible:
    name: str
    can_see_own: bool = False
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    json_mode: str = "schema"
    timeout: int = 600
    opener: Any = None

    # the chat completions url, https only unless the server is on this machine
    def _url(self):
        if not self.base_url or not self.model:
            raise ProviderError("set base_url and model for this provider")
        parts = urllib.parse.urlsplit(self.base_url)
        local = parts.scheme == "http" and parts.hostname in _LOOPBACK
        if parts.scheme != "https" and not local:
            raise ProviderError(f"base_url must use https, or http to localhost: {self.base_url}")
        return self.base_url.rstrip("/") + "/chat/completions"

    def _body(self, system, content, schema):
        if self.json_mode not in ("schema", "object", "prompt"):
            raise ProviderError(f"json_mode must be schema, object or prompt, not {self.json_mode}")
        if self.json_mode != "schema":
            system += ("\n\nAnswer with only a JSON object that matches this JSON Schema:\n"
                       + json.dumps(schema))
        body: dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content}
        ]}
        if self.json_mode == "schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": schema, "strict": True
            }}
        elif self.json_mode == "object":
            body["response_format"] = {"type": "json_object"}
        return body

    def ask(self, job, system, content, schema):
        url = self._url()
        headers = {"Content-Type": "application/json", "User-Agent": "syncret"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError(f"{self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(self._body(system, content, schema)).encode()
        # scheme checked in _url: https, or http to this machine only
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()[:300]
            raise ProviderError(f"{error.code}: {detail}",
                                retry=error.code == 429 or error.code >= 500) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ProviderError(f"cannot reach {url}: {reason}", retry=True) from error
        except json.JSONDecodeError as error:
            raise ProviderError("reply is not json", retry=True) from error
        try:
            choice = reply["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("reply has no choices") from error
        if message.get("refusal"):
            raise ProviderError(f"refused: {message['refusal']}")
        if choice.get("finish_reason") == "length":
            raise ProviderError("answer cut off at the token limit")
        text = (message.get("content") or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderError("answer is not json", retry=True) from error


# gemini through google's openai-compatible endpoint; model must be set in config
@dataclass
class Gemini(OpenAiCompatible):
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    api_key_env: str = "GEMINI_API_KEY"


# canned answers per job from a json file, for tests and dry runs
@dataclass
class Fake:
    name: str
    can_see_own: bool = False
    answers: str = ""
    calls: list = field(default_factory=list)

    def ask(self, job, system, content, schema):
        self.calls.append((job, content))
        answers = json.loads(Path(self.answers).read_text()) if self.answers else {}
        if job not in answers:
            raise ProviderError(f"no fake answer for {job}")
        return answers[job]


PROVIDERS = {
    "claude-code": ClaudeCode,
    "anthropic": AnthropicApi,
    "openai-compatible": OpenAiCompatible,
    "gemini": Gemini,
    "fake": Fake
}


# providers tried in order per job, with retries, fallback and the own-skill gate
@dataclass
class Llm:
    providers: dict
    jobs: dict = field(default_factory=dict)
    retries: int = 2
    sleep: Callable[[float], None] = time.sleep

    # the configured chain, or None when there is no config file
    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else default_config_path()
        if not path.is_file():
            return None
        try:
            config = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as error:
            raise LlmError(f"{path}: {error}") from error
        providers = {}
        for name, options in config.get("providers", {}).items():
            options = dict(options)
            kind = options.pop("type", "")
            if kind not in PROVIDERS:
                raise LlmError(f"{path}: provider {name!r} has unknown type {kind!r}")
            try:
                providers[name] = PROVIDERS[kind](name=name, **options)
            except TypeError as error:
                raise LlmError(f"{path}: provider {name!r}: {error}") from error
        jobs = config.get("jobs", {})
        for job, names in jobs.items():
            if job not in JOBS:
                raise LlmError(f"{path}: unknown job {job!r}, use one of {', '.join(JOBS)}")
            unknown = [n for n in names if n not in providers]
            if unknown:
                raise LlmError(f"{path}: job {job!r} lists unknown providers {unknown}")
        return cls(providers, jobs)

    # providers allowed for a job, in order; own content only goes to can_see_own providers
    def chain(self, job, own):
        names = self.jobs.get(job) or list(self.providers)
        return [self.providers[n] for n in names if not own or self.providers[n].can_see_own]

    # first valid answer along the chain; LlmError names every failure
    def ask(self, job, system, content, schema, own):
        chain = self.chain(job, own)
        if not chain:
            raise LlmError(f"no provider for {job} may see own skills (set can_see_own = true)")
        failures = []
        for provider in chain:
            for attempt in range(self.retries + 1):
                try:
                    data = provider.ask(job, system, content, schema)
                    if not matches(data, schema):
                        raise ProviderError("answer does not match the schema", retry=True)
                    return Answer(provider.name, data)
                except ProviderError as error:
                    if not error.retry or attempt == self.retries:
                        failures.append(f"{provider.name}: {error}")
                        break
                    self.sleep(min(2 ** attempt, 30))
        raise LlmError("every provider failed: " + "; ".join(failures))

    # ask every provider a fixed question with no skill content; (provider, error or None)
    def check(self):
        results = []
        for provider in self.providers.values():
            try:
                data = provider.ask("check", 'Reply with the JSON object {"ok": true}.',
                                    "Are you reachable?", CHECK_SCHEMA)
                results.append((provider, None if matches(data, CHECK_SCHEMA) else "bad answer"))
            except ProviderError as error:
                results.append((provider, str(error)))
        return results
