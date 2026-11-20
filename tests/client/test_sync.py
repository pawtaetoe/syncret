import fcntl
import hashlib
import io
import json
import os
import secrets
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "client" / "sync.sh"
TOKEN = "syncret_" + "t" * 43


# a release tarball like the server's: skills/<folder>/SKILL.md, links.tsv, manifest.json
def make_release(skills):
    buffer = io.BytesIO()
    lines = []
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, (claude, copilot) in skills.items():
            data = f"---\nname: {folder}\ndescription: x\n---\nBody.\n".encode()
            info = tarfile.TarInfo(f"skills/{folder}/SKILL.md")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            lines.append(f"{folder}\t{int(claude)}\t{int(copilot)}\t1\n")
        for name, text in (("links.tsv", "".join(lines)), ("manifest.json", json.dumps({}))):
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            archive.addfile(info, io.BytesIO(text.encode()))
    return secrets.token_hex(6), buffer.getvalue()


# a fake release server: /release/latest?format=text and /release/<id>.tar.gz with a token
class Server:
    def __init__(self):
        self.releases, self.latest, self.requests, self.status = {}, None, [], None
        self.bad_sha = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.reply(401, b"no")
                if outer.status:
                    return self.reply(outer.status, b"")
                if self.path == "/release/latest?format=text":
                    if outer.latest is None:
                        return self.reply(404, b"")
                    data = outer.releases[outer.latest]
                    digest = "0" * 64 if outer.bad_sha else hashlib.sha256(data).hexdigest()
                    return self.reply(200, f"{outer.latest} {digest} 60\n".encode())
                name = self.path.removeprefix("/release/").removesuffix(".tar.gz")
                if name in outer.releases:
                    return self.reply(200, outer.releases[name])
                return self.reply(404, b"")

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, skills):
        release_id, data = make_release(skills)
        self.releases[release_id] = data
        self.latest = release_id
        return release_id


@pytest.fixture
def server():
    srv = Server()
    yield srv
    srv.httpd.shutdown()


@pytest.fixture
def home(tmp_path, server):
    home = tmp_path / "home"
    config = home / ".config" / "syncret"
    config.mkdir(parents=True)
    (config / "client.env").write_text(f"SYNCRET_URL={server.url}/\nSYNCRET_TOKEN={TOKEN}\n")
    return home


def sync(home, *args, path=None):
    env = {"HOME": str(home), "PATH": path or os.environ["PATH"], "LANG": "C"}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True,
                          check=False)


def links(folder):
    return {p.name: os.readlink(p) for p in sorted(folder.iterdir())} if folder.is_dir() else {}


def data(home):
    return home / ".local" / "share" / "syncret"


# first sync: release unpacked read-only, ~/.agents/skills points at it, links per links.tsv
def test_first_sync(server, home):
    release = server.publish({"alpha": (1, 1), "both": (0, 1)})
    result = sync(home)
    assert result.returncode == 0, result.stderr
    agents = home / ".agents" / "skills"
    assert os.readlink(agents) == str(data(home) / "releases" / release / "skills")
    assert sorted(os.listdir(agents)) == ["alpha", "both"]
    assert links(home / ".claude" / "skills") == {"alpha": f"{agents}/alpha"}
    assert links(home / ".copilot" / "skills") == {"alpha": f"{agents}/alpha",
                                                  "both": f"{agents}/both"}
    with pytest.raises(PermissionError):
        (agents / "alpha" / "new.md").write_text("x")
    assert (home / ".claude" / "skills" / "alpha" / "SKILL.md").read_text().startswith("---")


# a run before the interval has passed asks nothing; --force asks anyway
def test_interval(server, home):
    server.publish({"alpha": (1, 1)})
    sync(home)
    asked = len(server.requests)
    assert sync(home).returncode == 0
    assert len(server.requests) == asked
    sync(home, "--force")
    assert len(server.requests) == asked + 1


# an update swaps the release, adds new links and removes ones for dropped skills
def test_update(server, home):
    server.publish({"alpha": (1, 1), "beta": (1, 0)})
    sync(home)
    second = server.publish({"beta": (1, 1), "gamma": (1, 1)})
    assert sync(home, "--force").returncode == 0
    agents = home / ".agents" / "skills"
    assert os.readlink(agents).endswith(f"/{second}/skills")
    assert set(links(home / ".claude" / "skills")) == {"beta", "gamma"}
    assert set(links(home / ".copilot" / "skills")) == {"beta", "gamma"}


# the user's own skills, foreign links and clashing folders are never touched
def test_leaves_user_files_alone(server, home, tmp_path):
    claude = home / ".claude" / "skills"
    (claude / "mine").mkdir(parents=True)
    (claude / "mine" / "SKILL.md").write_text("mine\n")
    (claude / "other").symlink_to(tmp_path)
    (claude / "alpha").mkdir()
    server.publish({"alpha": (1, 1)})
    result = sync(home)
    assert result.returncode == 0
    assert "leaving" in result.stderr
    assert (claude / "mine" / "SKILL.md").read_text() == "mine\n"
    assert os.readlink(claude / "other") == str(tmp_path)
    assert (claude / "alpha").is_dir()
    assert not (claude / "alpha").is_symlink()
    server.publish({"beta": (1, 1)})
    sync(home, "--force")
    assert (claude / "mine").is_dir()
    assert (claude / "other").is_symlink()


# a release that fails its sha256 check is not installed and the old one stays
def test_bad_sha_refused(server, home):
    first = server.publish({"alpha": (1, 1)})
    sync(home)
    server.publish({"beta": (1, 1)})
    server.bad_sha = True
    result = sync(home, "--force")
    assert result.returncode == 1
    assert "failed its sha256 check" in result.stderr
    assert os.readlink(home / ".agents" / "skills").endswith(f"/{first}/skills")
    assert sorted(os.listdir(data(home) / "releases")) == [first]


# a refused token is an error; nothing published yet is not
def test_server_answers(server, home):
    assert sync(home).returncode == 0
    assert "nothing published yet" in sync(home, "--force").stderr
    server.status = 401
    result = sync(home, "--force")
    assert result.returncode == 1
    assert "refused this machine's token" in result.stderr


# only the current release and the two newest others are kept
def test_keeps_three(server, home):
    for n in range(5):
        current = server.publish({f"s{n}": (1, 1)})
        sync(home, "--force")
    kept = os.listdir(data(home) / "releases")
    assert len(kept) == 3
    assert current in kept


# ~/.agents/skills as a real folder is never replaced
def test_refuses_real_agents_folder(server, home):
    (home / ".agents" / "skills").mkdir(parents=True)
    server.publish({"alpha": (1, 1)})
    result = sync(home)
    assert result.returncode == 1
    assert "is a real folder" in result.stderr
    assert (home / ".agents" / "skills").is_dir()


# the token never appears in curl's arguments, where other users could see it in ps
def test_token_not_in_arguments(server, home, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv"
    real = subprocess.run(["sh", "-c", "command -v curl"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (bin_dir / "curl").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {record}\nexec {real} "$@"\n')
    (bin_dir / "curl").chmod(0o755)
    server.publish({"alpha": (1, 1)})
    assert sync(home, path=f"{bin_dir}:{os.environ['PATH']}").returncode == 0
    assert TOKEN not in record.read_text()
    assert "/release/latest" in record.read_text()


# a second run while one holds the lock exits quietly
def test_lock(server, home):
    server.publish({"alpha": (1, 1)})
    sync(home)
    with (data(home) / "lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = sync(home, "--force")
    assert result.returncode == 0
    assert "another sync is running" in result.stderr


# spec test: while releases swap, a reader sees the old set or the new set, never a mix or none
def test_swap_is_atomic(server, home):
    one, two = {"alpha": (1, 1), "beta": (1, 1)}, {"gamma": (1, 1), "delta": (1, 1)}
    server.publish(one)
    sync(home)
    agents = home / ".agents" / "skills"
    seen, problems, done = set(), [], threading.Event()

    def reader():
        while not done.is_set():
            try:
                seen.add(frozenset(os.listdir(agents)))
            except OSError as error:
                problems.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    for n in range(12):
        server.publish(two if n % 2 == 0 else one)
        assert sync(home, "--force").returncode == 0
    done.set()
    thread.join()
    assert problems == []
    assert seen <= {frozenset({"alpha", "beta"}), frozenset({"gamma", "delta"})}
    assert len(seen) == 2
