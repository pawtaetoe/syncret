import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from syncret.cli import main
from syncret.core import upstream
from syncret.core.upstream import Upstream, UpstreamError

SKILLS = Path(__file__).parent / "fixtures" / "skills"


# run git in cwd with a throwaway identity
def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True
    )


# a local repo acme/skills standing in for github, with a good and a blocked skill
@pytest.fixture
def remote(tmp_path, monkeypatch):
    repo = tmp_path / "github" / "acme" / "skills"
    shutil.copytree(SKILLS / "good", repo / "skills" / "good")
    shutil.copytree(SKILLS / "remote-run-bad", repo / "skills" / "remote-run-bad")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "skills")
    monkeypatch.setattr(upstream, "GITHUB", str(tmp_path / "github" / "{owner}" / "{repo}"))
    return repo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/skills", Upstream("acme", "skills")),
        ("github.com/acme/skills.git", Upstream("acme", "skills")),
        ("https://github.com/acme/skills/tree/main/skills/good",
         Upstream("acme", "skills", "main", "skills/good")),
        ("https://github.com/acme/skills/blob/v1/skills/good/SKILL.md",
         Upstream("acme", "skills", "v1", "skills/good")),
        ("https://skills.sh/acme/skills/good", Upstream("acme", "skills", skill="good")),
        ("https://skills.sh/acme/skills", Upstream("acme", "skills")),
        ("https://github.com/acme/skills/tree/--upload-pack=touch${IFS}x/y", None),
        ("https://github.com/-acme/skills", None),
        ("https://gitlab.com/acme/skills", None),
        ("https://skills.sh/about", None)
    ]
)
def test_parse(url, expected):
    assert Upstream.parse(url) == expected


# a repo link vets every skill and links each at the pinned commit
def test_vet_repo(remote, capsys):
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=remote, capture_output=True, text=True,
        check=True
    ).stdout.strip()
    assert main(["vet", "https://github.com/acme/skills", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert sorted(skill["path"] for skill in data["skills"]) == [
        f"https://github.com/acme/skills/tree/{commit}/skills/good",
        f"https://github.com/acme/skills/tree/{commit}/skills/remote-run-bad"
    ]


# a tree link vets only that folder
def test_vet_tree_path(remote, capsys):
    assert main(["vet", "https://github.com/acme/skills/tree/main/skills/good"]) == 0
    assert "pass   good" in capsys.readouterr().out


# a skills.sh link vets the named skill from its github repo
def test_vet_skills_sh(remote, capsys):
    assert main(["vet", "https://skills.sh/acme/skills/remote-run-bad"]) == 1
    out = capsys.readouterr().out
    assert "block  remote-run-bad" in out
    assert "good" not in out


# missing skill, folder, repo, an escaping path and unsupported links exit 2, not 1
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://skills.sh/acme/skills/nope", "no skill 'nope'"),
        ("https://github.com/acme/skills/tree/main/nope", "no folder 'nope'"),
        ("https://github.com/acme/skills/tree/main/../..", "no folder"),
        ("https://github.com/acme/missing", "acme/missing"),
        ("https://gitlab.com/acme/skills", "unsupported link")
    ]
)
def test_vet_errors(remote, capsys, url, message):
    assert main(["vet", url]) == 2
    assert message in capsys.readouterr().err


# the clone is removed afterwards, also when vetting fails
def test_temp_dir_removed(remote, tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    main(["vet", "https://github.com/acme/skills"])
    main(["vet", "https://skills.sh/acme/skills/nope"])
    assert list(temp.iterdir()) == []


# a symlink in an upstream repo is checked out as a plain file, so files outside are never read
def test_symlinks_not_followed(remote, tmp_path, capsys):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET " + "QUJD1" * 60 + "\n")
    (remote / "skills" / "good" / "leak").symlink_to(secret)
    _git(remote, "add", ".")
    _git(remote, "commit", "-q", "-m", "link")
    main(["vet", "https://github.com/acme/skills/tree/main/skills/good", "--json"])
    assert "TOPSECRET" not in capsys.readouterr().out


# a link whose ref starts with "-" is refused by the parser, before git runs
def test_option_ref_link_refused(remote, capsys):
    url = "https://github.com/acme/skills/tree/--upload-pack=touch${IFS}pwned/skills"
    assert main(["vet", url]) == 2
    assert "unsupported link" in capsys.readouterr().err


# even past the parser, git reads the ref as a ref, so an injected option never runs;
# without --end-of-options git would run this upload-pack against the local fake upstream
def test_option_ref_not_run_by_git(remote, tmp_path):
    marker = tmp_path / "pwned"
    upstream = Upstream("acme", "skills", ref=f"--upload-pack=touch {marker}")
    with pytest.raises(UpstreamError):
        upstream.load()
    assert not marker.exists()
