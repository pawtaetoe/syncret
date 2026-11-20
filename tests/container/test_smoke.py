import io
import os
import re
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "skills" / "good"
PORT = "18090"
PASSWORD = "smoke test password 123"
PROJECT = ["docker", "compose", "-p", "syncret-smoke", "-f", str(ROOT / "compose.yaml")]

pytestmark = pytest.mark.skipif(os.environ.get("SYNCRET_CONTAINER_TEST") != "1",
                                reason="set SYNCRET_CONTAINER_TEST=1 to build and run the image")


def compose(*args, stdin=None, check=True):
    env = {**os.environ, "SYNCRET_PORT": PORT}
    return subprocess.run([*PROJECT, *args], input=stdin, capture_output=True, check=check,
                          env=env, cwd=ROOT)


def text(*args):
    return compose(*args).stdout.decode().strip()


@pytest.fixture(scope="module")
def server():
    compose("up", "-d", "--build", "--wait", "--wait-timeout", "120")
    try:
        yield httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30)
    finally:
        compose("down", "--volumes", check=False)


def csrf_of(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


# poll a job page until the job has finished
def wait_job(client, location):
    for _ in range(60):
        page = client.get(location).text
        if "badge\">done" in page or "badge\">failed" in page:
            return page
        time.sleep(1)
    raise AssertionError(f"{location} did not finish")


# the container is healthy, published on localhost only, and not root
def test_container_basics(server):
    assert server.get("/health").json() == {"status": "ok"}
    assert text("port", "syncret", "8080") == f"127.0.0.1:{PORT}"
    assert text("exec", "-T", "syncret", "id", "-u") == "1000"
    assert compose("exec", "-T", "syncret", "touch", "/app/x", check=False).returncode != 0


# a fixture skill added from a path in the container is vetted, approved and released
def test_add_approve_release(server):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(FIXTURE, arcname="good")
    compose("exec", "-T", "syncret", "mkdir", "-p", "/data/incoming")
    compose("exec", "-T", "syncret", "tar", "-x", "-C", "/data/incoming", stdin=buffer.getvalue())
    compose("exec", "-T", "syncret", "python", "-c",
            f"from syncret.server.auth import set_password; set_password('/data', {PASSWORD!r})")
    login = server.post("/login", data={"password": PASSWORD,
                                        "csrf": csrf_of(server.get("/login"))})
    assert (login.status_code, login.headers.get("location")) == (303, "/")
    token = csrf_of(server.get("/add"))
    added = server.post("/add", data={"source": "/data/incoming/good", "targets": "local",
                                      "csrf": token}, follow_redirects=False)
    assert added.status_code == 303
    assert "Proposal 1" in wait_job(server, added.headers["location"])
    for _ in range(60):
        detail = server.get("/proposals/1").text
        if "Approve and publish" in detail:
            break
        time.sleep(1)
    base = re.search(r'name="base" value="([0-9a-f]+)"', detail).group(1)
    approved = server.post("/proposals/1/approve", data={"base": base, "csrf": token},
                           follow_redirects=False)
    wait_job(server, approved.headers["location"])
    created = server.post("/machines", data={"name": "smoke", "csrf": token})
    machine = re.search(r"<pre>(syncret_[^<]+)</pre>", created.text).group(1)
    headers = {"Authorization": f"Bearer {machine}"}
    latest = None
    for _ in range(60):
        response = server.get("/release/latest", headers=headers)
        if response.status_code == 200:
            latest = response.json()
            break
        time.sleep(1)
    assert latest is not None
    release = server.get(latest["url"], headers=headers)
    with tarfile.open(fileobj=io.BytesIO(release.content)) as archive:
        assert "skills/good/SKILL.md" in archive.getnames()


# the image carries no data and no secrets: /data is empty and no token in its layers
def test_image_has_no_secrets(server):
    image = text("images", "-q", "syncret")
    assert subprocess.run(["docker", "run", "--rm", "--entrypoint", "ls", image, "-A", "/data"],
                          capture_output=True, text=True, check=True).stdout.strip() == ""
    history = subprocess.run(["docker", "history", "--no-trunc", image], capture_output=True,
                             text=True, check=True).stdout
    for secret in ("CLAUDE_CODE_OAUTH_TOKEN=", "sk-ant-", "syncret_mcp_", "password.json"):
        assert secret not in history
