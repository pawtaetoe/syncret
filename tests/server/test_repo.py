import json
import tarfile

import pytest

from syncret.server.repo import Repo, RepoLocked, Stale

ENTRY = {"name": "good", "kind": "own", "path": "skills/own/good", "source": "local",
         "pin": "", "targets": ["local"]}


@pytest.fixture
def repo(tmp_path):
    with Repo(tmp_path / "data").locked() as locked:
        locked.init()
        yield locked


# a new repo has an empty skills.json on main and published
def test_init(repo):
    assert repo.skills() == {"skills": {}}
    assert repo.head("main") == repo.head("published")
    first = repo.head()
    repo.init()
    assert repo.head() == first


# a second writer is refused while the first holds the lock, and writes need the lock
def test_single_writer(tmp_path):
    data = tmp_path / "data"
    with Repo(data).locked() as first:
        first.init()
        with pytest.raises(RepoLocked, match="another syncret worker"):
            Repo(data).locked().__enter__()
    with pytest.raises(RepoLocked, match="need the lock"):
        Repo(data).commit_tree(None, {}, "x")
    with Repo(data).locked() as again:
        assert again.skills() == {"skills": {}}


# a proposal is a commit on its own branch with the files and skills.json entry, main untouched
def test_propose(repo):
    base = repo.head()
    files = {"skills/own/good/SKILL.md": b"---\nname: good\n---\nBody.\n",
             "skills/own/good/run.sh": (b"#!/bin/sh\necho hi\n", 0o755)}
    commit = repo.propose("1", base, files, {"skills/own/good": ENTRY}, "add good")
    assert repo.head("proposal/1") == commit
    assert repo.head() == base
    assert repo.skills("proposal/1")["skills"] == {"skills/own/good": ENTRY}
    assert repo.files("proposal/1", "skills/own/good") == [
        "skills/own/good/SKILL.md", "skills/own/good/run.sh"
    ]
    modes = repo.text("ls-tree", "-r", "proposal/1", "--", "skills/own/good")
    assert "100755 blob" in modes.splitlines()[1]
    diff = repo.diff(base, "proposal/1")
    assert "+Body." in diff
    assert "skills/own/good/run.sh" in diff


# approval moves main only if it still points at base; a stale approval is refused
def test_approve_compare_and_swap(repo):
    base = repo.head()
    first = repo.propose("1", base, {"a.txt": b"a\n"}, {}, "one")
    second = repo.propose("2", base, {"b.txt": b"b\n"}, {}, "two")
    assert repo.approve("1", base) == first
    assert repo.head() == first
    with pytest.raises(Stale, match="main moved"):
        repo.approve("2", base)
    assert repo.head() == first
    assert repo.head("proposal/2") == second


# removing a skill removes every file under its folder and its skills.json entry
def test_remove(repo):
    base = repo.head()
    files = {"skills/own/good/SKILL.md": b"x\n", "skills/own/good/refs/a.md": b"y\n"}
    repo.propose("1", base, files, {"skills/own/good": ENTRY}, "add")
    repo.approve("1", base)
    main = repo.head()
    repo.propose("2", main, repo.removal(main, "skills/own/good"),
                 {"skills/own/good": None}, "remove")
    assert repo.files("proposal/2", "skills/own/good") == []
    assert repo.skills("proposal/2") == {"skills": {}}


# publish moves published with the same compare-and-swap; the release holds exactly that tree
def test_publish_and_archive(repo, tmp_path):
    old = repo.head("published")
    commit = repo.propose("1", repo.head(), {"skills/own/good/SKILL.md": b"x\n"},
                          {"skills/own/good": ENTRY}, "add")
    repo.approve("1", repo.head())
    repo.publish(commit, old)
    assert repo.head("published") == commit
    with pytest.raises(Stale):
        repo.publish(commit, old)
    release = tmp_path / "release.tar.gz"
    repo.archive("published", release)
    with tarfile.open(release) as archive:
        names = sorted(archive.getnames())
        assert names == ["skills", "skills.json", "skills/own", "skills/own/good",
                         "skills/own/good/SKILL.md"]
        member = archive.extractfile("skills.json")
        assert member is not None
        assert json.loads(member.read())["skills"] == {"skills/own/good": ENTRY}


# a skill folder can be unpacked from a ref for vetting
def test_export(repo, tmp_path):
    repo.propose("1", repo.head(), {"skills/own/good/SKILL.md": b"x\n"}, {}, "add")
    folder = repo.export("proposal/1", "skills/own/good", tmp_path / "out")
    assert (folder / "SKILL.md").read_text() == "x\n"


# dropping a proposal deletes its branch
def test_drop(repo):
    repo.propose("1", repo.head(), {"a.txt": b"a\n"}, {}, "one")
    repo.drop("1")
    with pytest.raises(RuntimeError):
        repo.head("proposal/1")
