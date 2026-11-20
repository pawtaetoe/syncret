import hashlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from syncret.server.app import create_app
from syncret.server.release import links

from .test_service import drain, make_skill


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (["local"], {"claude": True, "copilot": True, "zed": True}),
        (["account", "local"], {"claude": False, "copilot": True, "zed": True}),
        (["account"], {"claude": False, "copilot": False, "zed": False}),
        ([], {"claude": False, "copilot": False, "zed": False})
    ]
)
def test_links(targets, expected):
    assert links(targets) == expected


# a published release with three skills: local only, both places, account only
@pytest.fixture
def published(tmp_path):
    with TestClient(create_app(tmp_path / "data", start_worker=False)) as client:
        service = client.app.state.service
        drain(service)
        for name, targets in (("local-only", ["local"]), ("both", ["local", "account"]),
                              ("account-only", ["account"])):
            folder = make_skill(tmp_path / "src" / name, name, f"The {name} skill.")
            service.add(str(folder), targets=targets)
            drain(service)
            proposal = service.proposals.list(states=["open"])[0]
            service.approve(proposal.id, proposal.base)
            drain(service)
        yield client, service, service.machines.create("laptop")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# no token, a wrong token or a revoked one: refused (spec test)
def test_needs_machine_token(published):
    client, service, token = published
    assert client.get("/release/latest").status_code == 401
    assert client.get("/release/latest", headers=auth("syncret_wrong")).status_code == 401
    release = service.latest_release()["id"]
    assert client.get(f"/release/{release}.tar.gz").status_code == 401
    assert client.get("/release/latest", headers=auth(token)).status_code == 200
    service.machines.revoke(service.machines.check(token).id)
    assert client.get("/release/latest", headers=auth(token)).status_code == 401


# latest gives the id, the sha256 of the tarball and the sync interval
def test_latest_and_download(published):
    client, service, token = published
    service.settings.save(sync_interval_minutes=30)
    latest = client.get("/release/latest", headers=auth(token)).json()
    assert latest["interval_minutes"] == 30
    assert latest["url"] == f"/release/{latest['id']}.tar.gz"
    response = client.get(latest["url"], headers=auth(token))
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == latest["sha256"]
    machine = service.machines.list()[0]
    assert machine.release == latest["id"]
    assert machine.last_sync


# only skills for local agents ship; one targeted at both is not linked for claude code
def test_release_contents(published):
    client, _, token = published
    latest = client.get("/release/latest", headers=auth(token)).json()
    data = client.get(latest["url"], headers=auth(token)).content
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("manifest.json"))
        owners = {m.uname for m in archive.getmembers()}
    assert "skills/local-only/SKILL.md" in names
    assert "skills/both/SKILL.md" in names
    assert not any("account-only" in n for n in names)
    assert owners == {""}
    by_name = {s["name"]: s["links"] for s in manifest["skills"]}
    assert by_name == {"both": {"claude": False, "copilot": True, "zed": True},
                       "local-only": {"claude": True, "copilot": True, "zed": True}}


# bad or unknown release ids are refused without touching other files
@pytest.mark.parametrize(
    "release", ["..%2F..%2Fsecrets%2Fpassword", "ABCDEF123456", "000000000000"]
)
def test_bad_release_id(published, release):
    client, _, token = published
    assert client.get(f"/release/{release}.tar.gz", headers=auth(token)).status_code == 404


# nothing published yet: latest says so
def test_nothing_published(tmp_path):
    with TestClient(create_app(tmp_path / "data", start_worker=False)) as client:
        token = client.app.state.service.machines.create("laptop")
        assert client.get("/release/latest", headers=auth(token)).status_code == 404
