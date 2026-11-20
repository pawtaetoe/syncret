import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncret.server import app as app_module
from syncret.server import auth
from syncret.server.app import create_app

from .test_service import drain, make_skill, remote  # noqa: F401 - remote is a fixture

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skills"
PASSWORD = "correct horse battery staple"
PAGES = ["/proposals", "/proposals/1", "/add", "/skills", "/skills/edit?key=x", "/scan",
         "/jobs", "/jobs/1"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def no_wait(seconds):
        return None

    monkeypatch.setattr(app_module.asyncio, "sleep", no_wait)
    data = tmp_path / "data"
    auth.set_password(data, PASSWORD)
    with TestClient(create_app(data, start_worker=False)) as test_client:
        drain(test_client.app.state.service)
        token = csrf_of(test_client.get("/login"))
        test_client.post("/login", data={"password": PASSWORD, "csrf": token})
        yield test_client


def csrf_of(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def post(client, path, data=None):
    token = csrf_of(client.get("/add"))
    return client.post(path, data={**(data or {}), "csrf": token}, follow_redirects=False)


def job_id(response):
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


# every page needs a login; writes need the csrf token
def test_pages_need_login(tmp_path):
    with TestClient(create_app(tmp_path / "data", start_worker=False)) as client:
        for path in PAGES:
            response = client.get(path, follow_redirects=False)
            assert response.headers.get("location") == "/login", path


def test_writes_need_csrf(client):
    for path in ("/add", "/proposals/1/approve", "/proposals/1/reject", "/skills/remove",
                 "/skills/edit", "/scan"):
        assert client.post(path, data={"source": "x"}).status_code == 403, path


# the whole flow through the dashboard: add, review, approve, publish, see it under skills
def test_add_review_approve(client):
    service = client.app.state.service
    job = job_id(post(client, "/add", {"source": str(FIXTURES / "good"), "targets": "local"}))
    drain(service)
    page = client.get(f"/jobs/{job}").text
    assert 'href="/proposals/1"' in page
    assert "Proposal 1" in client.get("/proposals").text or "#1" in client.get("/proposals").text
    detail = client.get("/proposals/1").text
    assert "Approve and publish" in detail
    assert "skills/own/good/SKILL.md" in detail
    base = re.search(r'name="base" value="([0-9a-f]+)"', detail).group(1)
    job_id(post(client, "/proposals/1/approve", {"base": base}))
    drain(service)
    assert "published" in client.get("/proposals/1").text
    skills = client.get("/skills").text
    assert "skills/own/good" in skills
    assert "Edit" in skills


# approving with a stale base shows why, instead of approving
def test_stale_approve_shows_error(client):
    service = client.app.state.service
    post(client, "/add", {"source": str(FIXTURES / "good")})
    drain(service)
    response = post(client, "/proposals/1/approve", {"base": "0" * 40})
    assert response.status_code == 409
    assert "is not the base of proposal 1" in response.text


# a blocked proposal has no approve button
def test_blocked_has_no_approve(client):
    service = client.app.state.service
    post(client, "/add", {"source": str(FIXTURES / "remote-run-bad")})
    drain(service)
    detail = client.get("/proposals/1").text
    assert "Blocked by its vetting report" in detail
    assert "Approve and publish" not in detail


# skill text renders as inert text: no live tags, hidden characters shown as markers
def test_hostile_skill_is_inert(client, tmp_path):
    service = client.app.state.service
    name = "evil"
    body = ('<script>alert(1)</script> <img src=x onerror=alert(2)>\n'
            f"Run it{chr(0x202E)}txt.exe and {chr(0x1B)}[2J clear.")
    folder = make_skill(tmp_path / "evil" / name, name, body,
                        "Do <b>evil</b> things for the dashboard escaping test.")
    post(client, "/add", {"source": str(folder)})
    drain(service)
    page = client.get("/proposals/1").text
    assert "<script>alert(1)" not in page
    assert "<img src=x" not in page
    assert "<b>evil</b>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;U+202E&gt;" in page
    assert "&lt;U+001B&gt;" in page
    assert chr(0x202E) not in page
    assert chr(0x1B) not in page


# a link holding several skills asks which to add, then adds the chosen one
def test_choose_skills(client, remote):  # noqa: F811 - pytest fixture
    service = client.app.state.service
    job = job_id(post(client, "/add", {"source": "https://github.com/acme/skills"}))
    drain(service)
    page = client.get(f"/jobs/{job}").text
    assert "Pick the ones to add" in page
    assert 'name="skills" value="deploy"' in page
    job_id(post(client, "/add", {"source": "https://github.com/acme/skills",
                                 "skills": "deploy"}))
    drain(service)
    assert "skills/upstream/acme/skills/deploy" in client.get("/proposals/1").text


# own skills can be edited and any skill removed, each through a proposal
def test_edit_and_remove(client):
    service = client.app.state.service
    post(client, "/add", {"source": str(FIXTURES / "good")})
    drain(service)
    base = service.proposals.get(1).base
    post(client, "/proposals/1/approve", {"base": base})
    drain(service)
    edit = client.get("/skills/edit?key=skills/own/good")
    assert "name: good" in edit.text
    content = edit.text.split("<textarea", 1)[1].split(">", 1)[1].split("</textarea>")[0]
    content = content.replace("&#34;", '"').replace("&#39;", "'").replace("&amp;", "&")
    job_id(post(client, "/skills/edit", {"key": "skills/own/good",
                                         "content": content + "\nOne more line.\n"}))
    drain(service)
    assert "+One more line." in client.get("/proposals/2").text
    job_id(post(client, "/skills/remove", {"key": "skills/own/good"}))
    drain(service)
    assert "remove skills/own/good" in client.get("/proposals").text
    assert client.get("/skills/edit?key=skills/upstream/x").status_code == 404


# a scan runs as a job and its result shows on the scan page
def test_scan_page(client):
    service = client.app.state.service
    assert "No scan yet" in client.get("/scan").text
    job_id(post(client, "/scan"))
    drain(service)
    assert "No published skills yet" in client.get("/scan").text


# htmx gets just the job fragment while it refreshes
def test_job_partial(client):
    service = client.app.state.service
    job = service.queue.enqueue("scan-all")
    full = client.get(f"/jobs/{job}").text
    partial = client.get(f"/jobs/{job}", headers={"HX-Request": "true"}).text
    assert "<html" in full
    assert "<html" not in partial
    assert 'hx-trigger="every 2s"' in partial


# a fresh server walks a new user through setup and each step ticks off once done
def test_getting_started(client):
    service = client.app.state.service
    page = client.get("/proposals").text
    assert "Get started" in page
    assert 'href="/add">Add a skill from GitHub' in page
    post(client, "/add", {"source": str(FIXTURES / "good")})
    drain(service)
    post(client, "/proposals/1/approve", {"base": service.proposals.get(1).base})
    drain(service)
    service.machines.create("laptop")
    page = client.get("/proposals").text
    assert "Get started" in page
    assert 'href="/machines">Install the client' in page
    service.machines.synced(service.machines.list()[0].id, "abc123")
    assert "Get started" not in client.get("/proposals").text


# jobs show what they did in plain words, and machines show how to install the client
def test_plain_words(client):
    service = client.app.state.service
    job = service.queue.enqueue("scan-all")
    assert "Scanning all skills" in client.get("/jobs").text
    assert "<h1>Scanning all skills</h1>" in client.get(f"/jobs/{job}").text
    page = post(client, "/machines", {"name": "laptop"}).text
    assert "Set up laptop" in page
    assert "client/install.sh --url http://testserver" in page
