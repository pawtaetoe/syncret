import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from .test_sync import TOKEN, Server

INSTALL = Path(__file__).parent.parent.parent / "client" / "install.sh"


@pytest.fixture
def server():
    srv = Server()
    yield srv
    srv.httpd.shutdown()


# fake systemctl, launchctl and uname on PATH that record how they were called
@pytest.fixture
def fakes(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("systemctl", "launchctl"):
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {tmp_path}/calls\n')
        (bin_dir / name).chmod(0o755)
    return bin_dir


def install(home, *args, path, uname=None):
    bin_dir = Path(path.split(":")[0])
    if uname:
        (bin_dir / "uname").write_text(f"#!/bin/sh\necho {uname}\n")
        (bin_dir / "uname").chmod(0o755)
    env = {"HOME": str(home), "PATH": path, "LANG": "C"}
    return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True,
                          text=True, check=False)


def calls(tmp_path):
    path = tmp_path / "calls"
    return path.read_text().splitlines() if path.is_file() else []


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    return path


# linux: script, private config, units enabled, and a first sync
def test_install_linux(tmp_path, server, fakes, token_file):
    home = tmp_path / "home"
    server.publish({"alpha": (1, 1)})
    path = f"{fakes}:{os.environ['PATH']}"
    result = install(home, "--url", server.url + "/", "--token-file", str(token_file), path=path)
    assert result.returncode == 0, result.stderr
    binary = home / ".local" / "bin" / "syncret-sync"
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    config = home / ".config" / "syncret" / "client.env"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert config.read_text() == f"SYNCRET_URL={server.url}\nSYNCRET_TOKEN={TOKEN}\n"
    units = home / ".config" / "systemd" / "user"
    assert sorted(p.name for p in units.iterdir()) == ["syncret-sync.service",
                                                       "syncret-sync.timer"]
    assert calls(tmp_path) == ["systemctl --user daemon-reload",
                               "systemctl --user enable --now syncret-sync.timer"]
    assert (home / ".claude" / "skills" / "alpha").is_symlink()


# a reinstall without --url keeps the config; a bad token is refused
def test_reinstall_and_bad_token(tmp_path, server, fakes, token_file):
    home = tmp_path / "home"
    path = f"{fakes}:{os.environ['PATH']}"
    install(home, "--url", server.url, "--token-file", str(token_file), "--no-sync", path=path)
    config = home / ".config" / "syncret" / "client.env"
    before = config.read_text()
    assert install(home, "--no-sync", path=path).returncode == 0
    assert config.read_text() == before
    bad = tmp_path / "bad"
    bad.write_text("not a token\n")
    result = install(home, "--url", server.url, "--token-file", str(bad), "--no-sync", path=path)
    assert result.returncode == 2
    assert config.read_text() == before


# uninstall removes the timer and script and keeps the skills working
def test_uninstall(tmp_path, server, fakes, token_file):
    home = tmp_path / "home"
    server.publish({"alpha": (1, 1)})
    path = f"{fakes}:{os.environ['PATH']}"
    install(home, "--url", server.url, "--token-file", str(token_file), path=path)
    result = install(home, "--uninstall", path=path)
    assert result.returncode == 0
    assert not (home / ".local" / "bin" / "syncret-sync").exists()
    assert not (home / ".config" / "systemd" / "user" / "syncret-sync.timer").exists()
    assert "systemctl --user disable --now syncret-sync.timer" in calls(tmp_path)
    assert (home / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()


# macos: a valid launchd plist pointing at this home, bootstrapped into the user's domain
def test_install_macos_agent(tmp_path, server, fakes, token_file):
    home = tmp_path / "home"
    path = f"{fakes}:{os.environ['PATH']}"
    result = install(home, "--url", server.url, "--token-file", str(token_file), "--no-sync",
                     path=path, uname="Darwin")
    assert result.returncode == 0, result.stderr
    plist = home / "Library" / "LaunchAgents" / "com.syncret.sync.plist"
    agent = plistlib.loads(plist.read_bytes())
    assert agent["Label"] == "com.syncret.sync"
    assert agent["ProgramArguments"] == [f"{home}/.local/bin/syncret-sync"]
    assert agent["StartInterval"] == 900
    assert agent["StandardErrorPath"] == f"{home}/Library/Logs/syncret-sync.log"
    uid = os.getuid()
    assert calls(tmp_path) == [f"launchctl bootout gui/{uid} {plist}",
                               f"launchctl bootstrap gui/{uid} {plist}"]
