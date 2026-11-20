import io
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from syncret.core import llm as llm_module
from syncret.core.llm import ClaudeCode
from syncret.server.service import Service
from syncret.server.settings import Settings

from .test_pages import client, csrf_of, job_id, post  # noqa: F401 - client is a fixture
from .test_service import drain, make_skill

TOKEN = "sk-ant-oat01-" + "x" * 40


# intervals have defaults and limits
def test_intervals(tmp_path):
    settings = Settings(tmp_path)
    assert settings.load() == {"check_interval_hours": 6, "sync_interval_minutes": 60}
    assert settings.save(sync_interval_minutes=30)["sync_interval_minutes"] == 30
    assert Settings(tmp_path).load()["sync_interval_minutes"] == 30
    with pytest.raises(ValueError, match="from 5 to 1440"):
        settings.save(sync_interval_minutes=1)


# llm.toml is replaced only by text that loads
def test_save_llm(tmp_path):
    settings = Settings(tmp_path)
    assert settings.save_llm('[providers.claude]\ntype = "claude-code"\n') == ["claude"]
    with pytest.raises(ValueError, match="unknown type 'nope'") as info:
        settings.save_llm('[providers.x]\ntype = "nope"\n')
    assert str(tmp_path) not in str(info.value)
    assert "claude-code" in settings.llm_text()


# the token is stored privately, never returned, and handed to claude through its environment
def test_claude_token(tmp_path, monkeypatch):
    service = Service(tmp_path)
    with pytest.raises(ValueError, match="does not look like"):
        service.settings.save_token("short")
    service.settings.save_token(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    path = service.settings.token_path
    assert path.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    info = service.settings.token_info()
    assert info["days"] == 0
    assert TOKEN not in json.dumps(info)
    service.settings.save_llm('[providers.claude]\ntype = "claude-code"\n')
    [provider] = service.llm().providers.values()
    assert isinstance(provider, ClaudeCode)
    assert provider.token_file == str(path)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        reply = {"is_error": False, "structured_output": {"ok": True}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    monkeypatch.setattr(llm_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm_module.subprocess, "run", run)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert provider.ask("check", "s", "c", schema) == {"ok": True}
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    missing = ClaudeCode("cc", token_file=str(tmp_path / "missing"))
    with pytest.raises(llm_module.ProviderError, match="does not exist"):
        missing.ask("check", "s", "c", schema)


# machine tokens: shown once, stored as a hash, revoked at once
def test_machines(tmp_path):
    service = Service(tmp_path)
    token = service.machines.create("laptop")
    assert token.startswith("syncret_")
    assert token not in (tmp_path / "syncret.db").read_bytes().decode(errors="replace")
    machine = service.machines.check(token)
    assert machine.name == "laptop"
    assert service.machines.check("syncret_wrong") is None
    assert service.machines.check("") is None
    service.machines.synced(machine.id, "abc123")
    assert service.machines.list()[0].release == "abc123"
    service.machines.revoke(machine.id)
    assert service.machines.check(token) is None
    with pytest.raises(ValueError):
        service.machines.create("  ")


# the settings page saves things but never shows the token again
def test_settings_page(client):  # noqa: F811 - pytest fixture
    page = client.get("/settings").text
    assert "No token saved" in page
    response = post(client, "/settings/token", {"token": TOKEN})
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert "Claude token saved" in response.text
    assert TOKEN not in client.get("/settings").text
    response = post(client, "/settings/llm", {"llm": '[providers.x]\ntype = "nope"\n'})
    assert response.status_code == 400
    assert "unknown type" in response.text
    response = post(client, "/settings/llm", {"llm": '[providers.claude]\ntype = "claude-code"\n'
                                                      "can_see_own = true\n"})
    assert "Providers saved: claude" in response.text
    assert "may see them" in response.text
    response = post(client, "/settings/intervals", {"check_interval_hours": "12",
                                                    "sync_interval_minutes": "abc"})
    assert response.status_code == 400
    job = job_id(post(client, "/settings/llm-check"))
    assert client.app.state.service.queue.get(job).kind == "llm-check"


# the machines page shows a new token once, then only its name
def test_machines_page(client):  # noqa: F811 - pytest fixture
    response = post(client, "/machines", {"name": "laptop"})
    token = re.search(r"<pre>(syncret_[^<]+)</pre>", response.text).group(1)
    assert client.app.state.service.machines.check(token) is not None
    listing = client.get("/machines").text
    assert "laptop" in listing
    assert token not in listing
    post(client, "/machines/1/revoke")
    assert client.app.state.service.machines.check(token) is None
    assert "revoked" in client.get("/machines").text


# account skills: zip with the skill in a named folder, out of date until marked, again after
# an update
def test_account_page(client, tmp_path):  # noqa: F811 - pytest fixture
    service = client.app.state.service
    folder = make_skill(tmp_path / "src" / "notes", "notes", "Take notes.")
    post(client, "/add", {"source": str(folder), "targets": ["local", "account"]})
    drain(service)
    service.approve(1, service.proposals.get(1).base)
    drain(service)
    page = client.get("/account").text
    assert "out of date" in page
    response = client.get("/account/zip?key=skills/own/notes")
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="notes.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "notes/SKILL.md" in archive.namelist()
    post(client, "/account/done", {"key": "skills/own/notes"})
    assert "uploaded" in client.get("/account").text
    (folder / "SKILL.md").write_text((folder / "SKILL.md").read_text() + "More.\n")
    post(client, "/add", {"source": str(folder), "targets": ["local", "account"]})
    drain(service)
    update = service.proposals.list(states=["open"])[0]
    service.approve(update.id, update.base)
    drain(service)
    assert "out of date" in client.get("/account").text
    assert client.get("/account/zip?key=skills/own/missing").status_code == 404


# the llm check job reports each provider, or that none is configured
def test_llm_check_job(tmp_path):
    service = Service(tmp_path)
    job = service.llm_check()
    drain(service)
    assert service.queue.get(job).result == {"providers": [], "note": "no LLM configured"}
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"check": {"ok": True}}))
    service.settings.save_llm(f'[providers.fake]\ntype = "fake"\nanswers = "{answers}"\n')
    job = service.llm_check()
    drain(service)
    assert service.queue.get(job).result["providers"] == [
        {"name": "fake", "ok": True, "error": None, "can_see_own": False}
    ]
    assert Path(answers).is_file()


# syncret set-claude-token reads the token silently and stores it
def test_cli_set_claude_token(tmp_path, monkeypatch, capsys):
    from syncret.cli import main

    answers = iter([TOKEN, "short"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert main(["set-claude-token", "--data", str(tmp_path)]) == 0
    assert Settings(tmp_path).token_info()["days"] == 0
    assert TOKEN not in capsys.readouterr().out
    assert main(["set-claude-token", "--data", str(tmp_path)]) == 2


# llm check beside a server data folder checks the server's providers with its saved token
def test_cli_llm_check_server_providers(tmp_path, monkeypatch, capsys):
    from syncret.cli import main

    service = Service(tmp_path / "data")
    service.settings.save_token(TOKEN)
    service.settings.save_llm('[providers.claude]\ntype = "claude-code"\ncan_see_own = true\n')
    seen = []

    def run(argv, **kwargs):
        seen.append((kwargs["env"] or {}).get("CLAUDE_CODE_OAUTH_TOKEN"))
        reply = {"is_error": False, "structured_output": {"ok": True}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    monkeypatch.setattr(llm_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm_module.subprocess, "run", run)
    assert main(["llm", "check", "--data", str(tmp_path / "data")]) == 0
    assert "ok    claude" in capsys.readouterr().out
    monkeypatch.setenv("SYNCRET_DATA", str(tmp_path / "data"))
    assert main(["llm", "check"]) == 0
    assert seen == [TOKEN, TOKEN]
    user = tmp_path / "user.toml"
    user.write_text('[providers.mine]\ntype = "claude-code"\n')
    assert main(["llm", "check", "--config", str(user)]) == 0
    assert "ok    mine" in capsys.readouterr().out
    assert seen == [TOKEN, TOKEN, None]


# a data folder without providers falls back to the user's own config
def test_cli_llm_check_no_server_providers(tmp_path, capsys):
    from syncret.cli import main

    assert main(["llm", "check", "--data", str(tmp_path / "empty")]) == 2
    assert "no LLM configured" in capsys.readouterr().err
