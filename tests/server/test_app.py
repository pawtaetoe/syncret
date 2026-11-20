import asyncio
import json
import re
import stat

import pytest
from fastapi.testclient import TestClient

from syncret.cli import main
from syncret.server import app as app_module
from syncret.server import auth
from syncret.server.app import create_app

PASSWORD = "correct horse battery staple"


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def client(data, monkeypatch):
    async def no_wait(seconds):
        return None

    monkeypatch.setattr(app_module.asyncio, "sleep", no_wait)
    auth.set_password(data, PASSWORD)
    with TestClient(create_app(data, start_worker=False)) as test_client:
        yield test_client


def csrf_of(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, "no csrf field on the page"
    return match.group(1)


def login(client, password=PASSWORD):
    token = csrf_of(client.get("/login"))
    return client.post("/login", data={"password": password, "csrf": token},
                       follow_redirects=False)


# health needs no login; a stopped worker makes it fail, so the container restarts
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    loop = asyncio.new_event_loop()
    done = loop.create_future()
    done.set_result(None)
    client.app.state.worker = done
    response = client.get("/health")
    assert response.status_code == 503
    loop.close()


# pages need a session: browsers are sent to the login page, htmx gets a redirect header
def test_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert (response.status_code, response.headers["hx-redirect"]) == (401, "/login")


# the right password logs in with a strict, http-only cookie; a wrong one does not
def test_login(client):
    assert login(client, "wrong password").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    response = login(client)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/", follow_redirects=False).headers["location"] == "/proposals"


# forms without the session's csrf token are refused, logging in included
def test_csrf(client):
    client.get("/login")
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 403
    response = client.post("/login", data={"password": PASSWORD, "csrf": "forged"},
                           follow_redirects=False)
    assert response.status_code == 403
    login(client)
    assert client.post("/logout", data={"csrf": "forged"}).status_code == 403
    token = csrf_of(client.get("/login"))
    response = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


# without a password set, the login page says how to set one and nothing logs in
def test_no_password(data):
    with TestClient(create_app(data, start_worker=False)) as client:
        assert "syncret set-password" in client.get("/login").text
        assert login(client, "anything at all").status_code == 401


# every response carries the security headers
def test_security_headers(client):
    headers = client.get("/login").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


# the vendored htmx is served by syncret itself
def test_static(client):
    assert client.get("/static/htmx.min.js").text.startswith("var htmx=")


# the password is stored as an scrypt hash in a private file; short ones are refused
def test_password_storage(data):
    auth.set_password(data, PASSWORD)
    path = data / "secrets" / "password.json"
    record = json.loads(path.read_text())
    assert PASSWORD not in path.read_text()
    assert set(record) == {"salt", "hash", "n", "r", "p"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "secrets").stat().st_mode) == 0o700
    assert auth.verify_password(PASSWORD, record)
    assert not auth.verify_password(PASSWORD + "x", record)
    with pytest.raises(ValueError, match="at least 12"):
        auth.set_password(data, "short")


# syncret set-password asks twice and stores the hash
def test_cli_set_password(data, monkeypatch, capsys):
    answers = iter([PASSWORD, PASSWORD, PASSWORD, "different password!"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert main(["set-password", "--data", str(data)]) == 0
    assert auth.verify_password(PASSWORD, auth.password_record(data))
    assert main(["set-password", "--data", str(data)]) == 2
    assert "differ" in capsys.readouterr().err
