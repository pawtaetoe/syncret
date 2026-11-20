import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncret.server.app import create_app
from syncret.server.mcp import new_token

from .test_pages import client, post  # noqa: F401 - client is a fixture
from .test_service import drain

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skills"
TOOLS = {"list_skills", "list_proposals", "show_proposal", "approve", "reject", "add_skill",
         "remove_skill", "propose_edit", "propose_merge", "scan", "job_status"}


# an mcp client over the test client: json-rpc posts with the bearer token
class Mcp:
    def __init__(self, http, token):
        self.client, self.token, self.id = http, token, 0

    def rpc(self, method, params=None, token=None, headers=None):
        self.id += 1
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": self.id, "method": method,
                                              "params": params or {}},
                                headers={"Accept": "application/json, text/event-stream",
                                         "Authorization": f"Bearer {token or self.token}",
                                         **(headers or {})})

    # a tool's structured result, or the error text when the tool refused
    def call(self, tool, **arguments):
        reply = self.rpc("tools/call", {"name": tool, "arguments": arguments}).json()["result"]
        if reply["isError"]:
            return {"error": " ".join(c.get("text", "") for c in reply["content"])}
        return reply["structuredContent"]


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCRET_ALLOWED_HOSTS", "testserver")
    data = tmp_path / "data"
    token = new_token(data)
    with TestClient(create_app(data, start_worker=False)) as test_client:
        drain(test_client.app.state.service)
        session = Mcp(test_client, token)
        init = session.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}})
        assert init.status_code == 200
        yield session


# no token, a wrong token, or a host that is not allowed: refused before any tool runs
def test_access(mcp):
    assert mcp.rpc("tools/list", token="syncret_mcp_wrong").status_code == 401
    response = mcp.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert mcp.rpc("tools/list", headers={"Host": "evil.example"}).status_code == 421


# a new token replaces the old one
def test_token_rotation(mcp):
    assert mcp.rpc("tools/list").status_code == 200
    data = mcp.client.app.state.service.data
    fresh = new_token(data)
    assert mcp.rpc("tools/list").status_code == 401
    assert mcp.rpc("tools/list", token=fresh).status_code == 200


# the spec's tools are all there, with the warnings in their descriptions
def test_tools(mcp):
    tools = {t["name"]: t for t in mcp.rpc("tools/list").json()["result"]["tools"]}
    assert set(tools) == TOOLS
    assert "untrusted" in tools["show_proposal"]["description"]
    assert "person has said to approve" in tools["approve"]["description"]


# the whole flow through mcp: add, review, a stale approve refused, approve, publish, remove
def test_flow(mcp):
    service = mcp.client.app.state.service
    job = mcp.call("add_skill", source=str(FIXTURES / "good"))["job"]
    drain(service)
    assert mcp.call("job_status", id=job)["state"] == "done"
    [listed] = mcp.call("list_proposals")["result"]
    assert (listed["kind"], listed["state"], listed["verdicts"]) == ("add", "open", ["pass"])
    shown = mcp.call("show_proposal", id=listed["id"])
    assert "skills/own/good/SKILL.md" in shown["diff"]
    assert "untrusted" in shown["warning"]
    refused = mcp.call("approve", id=listed["id"], base="0" * 40)
    assert "is not the base" in refused["error"]
    mcp.call("approve", id=listed["id"], base=listed["base"])
    drain(service)
    skills = mcp.call("list_skills")["result"]
    assert [s["key"] for s in skills] == ["skills/own/good"]
    mcp.call("remove_skill", name="good")
    drain(service)
    assert mcp.call("list_proposals")["result"][0]["kind"] == "remove"
    assert "no single published skill" in mcp.call("remove_skill", name="nope")["error"]
    scan = mcp.call("scan")
    drain(service)
    assert mcp.call("job_status", id=scan["job"])["result"]["skills"][0]["path"] == \
        "skills/own/good"
    assert "no job" in mcp.call("job_status", id=999)["error"]


# writes through mcp only queue jobs: nothing changes until the worker runs
def test_writes_only_queue(mcp):
    service = mcp.client.app.state.service
    before = service.repo.head()
    mcp.call("add_skill", source=str(FIXTURES / "good"))
    assert service.repo.head() == before
    assert service.proposals.list() == []
    assert [j.kind for j in service.queue.jobs()] == ["add"]


# the settings page makes an mcp token and shows it once
def test_settings_token(client):  # noqa: F811 - pytest fixture
    page = client.get("/settings").text
    assert "No token yet, so MCP is closed" in page
    response = post(client, "/settings/mcp-token")
    token = re.search(r"<pre>(syncret_mcp_[^<]+)</pre>", response.text).group(1)
    assert token not in client.get("/settings").text
    assert "A token is set" in client.get("/settings").text
    stored = (client.app.state.service.data / "secrets" / "mcp-token.json").read_text()
    assert token not in stored
    assert set(json.loads(stored)) == {"sha256"}
