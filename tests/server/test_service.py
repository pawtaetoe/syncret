import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from syncret.core import upstream
from syncret.server.service import Refused, Service
from syncret.server.worker import Worker

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skills"


# run every queued job, including ones the jobs queue, until the queue is empty
def drain(service):
    with service.repo.locked():
        service.repo.init()
        worker = Worker(service.queue, service.repo, service.handlers)
        while worker.run_once():
            pass


# a skill folder with a name, description and body
def make_skill(root, name, body, description=None):
    root.mkdir(parents=True, exist_ok=True)
    description = description or f"Handle {name} tasks for the merge and scan tests here."
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    )
    return root


@pytest.fixture
def service(tmp_path):
    svc = Service(tmp_path / "data")
    drain(svc)
    return svc


def _only(service, **filters):
    proposals = [p for p in service.proposals.list()
                 if all(getattr(p, k) == v for k, v in filters.items())]
    assert len(proposals) == 1, proposals
    return proposals[0]


# a fake llm config in the data folder answering each job
def fake_llm(service, answers, can_see_own=True):
    path = service.data / "answers.json"
    path.write_text(json.dumps(answers))
    (service.data / "llm.toml").write_text(
        f'[providers.fake]\ntype = "fake"\nanswers = "{path}"\n'
        f"can_see_own = {'true' if can_see_own else 'false'}\n"
    )


# add from a folder on the server: an own skill proposal, vetted, scanned, open
def test_add_own(service):
    job = service.add(str(FIXTURES / "good"))
    drain(service)
    assert service.queue.get(job).state == "done"
    proposal = _only(service, kind="add")
    assert proposal.state == "open"
    assert proposal.skills == ["skills/own/good"]
    assert [r["verdict"] for r in proposal.vet] == ["pass"]
    assert proposal.vet[0]["path"] == "skills/own/good"
    assert proposal.scan == {"duplicates": [], "collisions": [], "overlaps": [], "notes": []}
    _, diff = service.show(proposal.id)
    assert "skills/own/good/SKILL.md" in diff
    assert "skills/own/good/scripts/summarise.sh" in diff
    entry = service.repo.skills(f"proposal/{proposal.id}")["skills"]["skills/own/good"]
    assert (entry["kind"], entry["source"], entry["targets"]) == ("own", "local", ["local"])
    assert service.repo.skills() == {"skills": {}}


# approve moves main, publish packs a release with a matching sha256 and marks it published
def test_approve_and_publish(service):
    service.add(str(FIXTURES / "good"))
    drain(service)
    proposal = _only(service, kind="add")
    service.approve(proposal.id, proposal.base)
    drain(service)
    assert service.proposals.get(proposal.id).state == "published"
    assert "skills/own/good" in service.repo.skills("published")["skills"]
    latest = service.latest_release()
    release = service.releases / f"{latest['id']}.tar.gz"
    assert latest["commit"] == service.repo.head("published")
    assert latest["sha256"] == hashlib.sha256(release.read_bytes()).hexdigest()
    with tarfile.open(release) as archive:
        script = archive.getmember("skills/good/scripts/summarise.sh")
        assert script.mode & 0o111
        assert "skills/good/SKILL.md" in archive.getnames()
        manifest = json.load(archive.extractfile("manifest.json"))
    assert manifest["commit"] == latest["commit"]
    assert manifest["skills"] == [{"folder": "good", "key": "skills/own/good", "name": "good",
                                   "kind": "own",
                                   "links": {"claude": True, "copilot": True, "zed": True}}]


# a stale approval is refused at once and rebuilt on the new main
def test_stale_approval_refused(service, tmp_path):
    service.add(str(FIXTURES / "good"))
    service.add(str(make_skill(tmp_path / "x" / "other", "other", "Do other things.")))
    drain(service)
    first, second = sorted(service.proposals.list(), key=lambda p: p.id)
    assert first.base == second.base
    service.approve(first.id, first.base)
    drain(service)
    with pytest.raises(Refused, match="stale"):
        service.approve(second.id, second.base)
    drain(service)
    assert service.proposals.get(second.id).state == "stale"
    rebuilt = [p for p in service.proposals.list() if p.id > second.id]
    assert len(rebuilt) == 1
    assert rebuilt[0].state == "open"
    assert rebuilt[0].base == service.repo.head()


# two approvals queued from the same base: git's compare-and-swap lets only the first through
def test_stale_caught_inside_the_job(service, tmp_path):
    service.add(str(FIXTURES / "good"))
    service.add(str(make_skill(tmp_path / "x" / "other", "other", "Do other things.")))
    drain(service)
    first, second = sorted(service.proposals.list(), key=lambda p: p.id)
    service.approve(first.id, first.base)
    service.approve(second.id, second.base)
    drain(service)
    assert service.proposals.get(first.id).state == "published"
    assert service.proposals.get(second.id).state == "stale"
    published = service.repo.skills("published")["skills"]
    assert "skills/own/good" in published
    assert "skills/own/other" not in published


# a blocked proposal cannot be approved
def test_blocked_refused(service):
    service.add(str(FIXTURES / "remote-run-bad"))
    drain(service)
    proposal = _only(service, kind="add")
    assert proposal.blocked
    with pytest.raises(Refused, match="blocked"):
        service.approve(proposal.id, proposal.base)


# requests that make no sense are refused before anything is queued
def test_request_checks(service):
    with pytest.raises(Refused):
        service.add("  ")
    with pytest.raises(Refused, match="targets"):
        service.add(str(FIXTURES / "good"), targets=["phone"])
    with pytest.raises(Refused, match="no proposal"):
        service.approve(99, "abc")
    with pytest.raises(Refused, match="no skill"):
        service.remove("skills/own/nope")
    assert service.queue.jobs() == []


# a bad source fails its job with a clear error
def test_bad_source_fails(service, tmp_path):
    job = service.add(str(tmp_path / "missing"))
    drain(service)
    assert service.queue.get(job).state == "failed"
    assert "not a supported link or a folder" in service.queue.get(job).error


# a fake github repo with two skills, for link sources
@pytest.fixture
def remote(tmp_path, monkeypatch):
    repo = tmp_path / "github" / "acme" / "skills"
    shutil.copytree(FIXTURES / "good", repo / "skills" / "good")
    make_skill(repo / "skills" / "deploy", "deploy", "- Never deploy on Fridays.")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=repo, check=True)
    monkeypatch.setattr(upstream, "GITHUB", str(tmp_path / "github" / "{owner}" / "{repo}"))
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


# a link with several skills asks which to add; a chosen one is pinned under upstream
def test_add_from_link(service, remote):
    job = service.add("https://github.com/acme/skills")
    drain(service)
    assert service.queue.get(job).result == {"choose": ["deploy", "good"]}
    assert service.proposals.list() == []
    service.add("https://github.com/acme/skills", skills=["deploy"], targets=["local", "account"])
    drain(service)
    proposal = _only(service, kind="add")
    key = "skills/upstream/acme/skills/deploy"
    assert proposal.skills == [key]
    entry = service.repo.skills(f"proposal/{proposal.id}")["skills"][key]
    assert (entry["kind"], entry["pin"], entry["targets"]) == ("upstream", remote,
                                                               ["account", "local"])
    assert entry["source"].endswith(f"/tree/{remote}/skills/deploy")


# adding a skill again is an update, and files it no longer has are removed
def test_update(service, tmp_path):
    folder = make_skill(tmp_path / "src" / "notes", "notes", "Take notes.")
    (folder / "old.md").write_text("old\n")
    service.add(str(folder))
    drain(service)
    first = _only(service, kind="add")
    service.approve(first.id, first.base)
    drain(service)
    (folder / "old.md").unlink()
    (folder / "SKILL.md").write_text((folder / "SKILL.md").read_text() + "More notes.\n")
    service.add(str(folder))
    drain(service)
    update = _only(service, kind="update")
    assert service.repo.files(f"proposal/{update.id}", "skills/own/notes") == [
        "skills/own/notes/SKILL.md"
    ]
    _, diff = service.show(update.id)
    assert "+More notes." in diff
    assert "old.md" in diff


# remove and edit go through proposals; only own skills can be edited
def test_remove_and_edit(service, remote):
    for job in (lambda: service.add("https://github.com/acme/skills", skills=["deploy"]),
                lambda: service.add(str(FIXTURES / "good"))):
        job()
        drain(service)
        proposal = service.proposals.list(states=["open"])[0]
        service.approve(proposal.id, proposal.base)
        drain(service)
    with pytest.raises(Refused, match="only own skills"):
        service.edit("skills/upstream/acme/skills/deploy", "x")
    service.edit("skills/own/good", "---\nname: good\ndescription: Format CSV exports from "
                                    "the billing system into tables.\n---\nNew body.\n")
    drain(service)
    edit = _only(service, kind="edit")
    assert edit.state == "open"
    assert [r["verdict"] for r in edit.vet] == ["pass"]
    service.remove("skills/upstream/acme/skills/deploy")
    drain(service)
    removal = _only(service, kind="remove")
    assert removal.state == "open"
    service.approve(removal.id, removal.base)
    drain(service)
    assert "skills/upstream/acme/skills/deploy" not in service.repo.skills("published")["skills"]


# rejecting drops the proposal branch
def test_reject(service):
    service.add(str(FIXTURES / "good"))
    drain(service)
    proposal = _only(service, kind="add")
    service.reject(proposal.id)
    drain(service)
    assert service.proposals.get(proposal.id).state == "rejected"
    with pytest.raises(RuntimeError):
        service.repo.head(f"proposal/{proposal.id}")


# an add that overlaps a published skill gets a suggested merge proposal; sources lose targets
def test_overlap_suggests_merge(service, tmp_path):
    rules_a = "- Never commit secrets.\n- Always sign commits."
    rules_b = "- Never push to main.\n- Always sign commits."
    fake_llm(service, {
        "review": {"concerns": []},
        "overlap": {"label": "overlapping", "summary": "Both cover commits.", "disagreements": []},
        "merge": {"description": "Commit safely: sign every commit and keep secrets out.",
                  "body": "- Never commit secrets.\n- Always sign commits.\n"
                          "- Never push to main.\n", "dropped": []}
    })
    service.add(str(make_skill(tmp_path / "a" / "commits", "commits", rules_a,
                               "Make safe git commits for the team repository.")))
    drain(service)
    first = _only(service, kind="add")
    service.approve(first.id, first.base)
    drain(service)
    service.add(str(make_skill(tmp_path / "b" / "git-safety", "git-safety", rules_b,
                               "Make safe git commits for the shared repository.")))
    drain(service)
    add = next(p for p in service.proposals.list() if p.kind == "add" and p.id != first.id)
    assert add.scan["overlaps"][0]["label"] == "overlapping"
    merge = _only(service, kind="merge")
    assert merge.state == "open"
    assert any(f"suggested merge: proposal {merge.id}" == n for n in add.notes)
    entries = service.repo.skills(f"proposal/{merge.id}")["skills"]
    assert entries["skills/merged/commits"]["targets"] == ["local"]
    assert entries["skills/merged/commits"]["sources"] == ["skills/own/git-safety",
                                                           "skills/own/commits"]
    assert entries["skills/own/commits"]["targets"] == []
    assert entries["skills/own/git-safety"]["targets"] == []


# without an llm, overlaps stay unconfirmed and no merge is queued
def test_no_llm_no_merge(service, tmp_path):
    service.add(str(make_skill(tmp_path / "a" / "commits", "commits", "- Always sign commits.",
                               "Make safe git commits for the team repository.")))
    drain(service)
    first = _only(service, kind="add")
    service.approve(first.id, first.base)
    drain(service)
    service.add(str(make_skill(tmp_path / "b" / "git-safety", "git-safety",
                               "- Always sign commits.",
                               "Make safe git commits for the shared repository.")))
    drain(service)
    assert [p.kind for p in service.proposals.list()].count("merge") == 0
    added = next(p for p in service.proposals.list() if p.kind == "add" and p.id != first.id)
    assert added.scan["overlaps"][0]["label"] == "unconfirmed"


# own skills are not reviewed by a provider that may not see them
def test_privacy(service):
    fake_llm(service, {"review": {"concerns": []}}, can_see_own=False)
    service.add(str(FIXTURES / "good"))
    drain(service)
    proposal = _only(service, kind="add")
    assert any("llm review skipped" in n and "can_see_own" in n for n in proposal.notes)


# a full scan of published skills reports with repo paths
def test_scan_all(service):
    service.add(str(FIXTURES / "good"))
    drain(service)
    proposal = _only(service, kind="add")
    service.approve(proposal.id, proposal.base)
    drain(service)
    job = service.scan_all()
    drain(service)
    result = service.queue.get(job).result
    assert [s["path"] for s in result["skills"]] == ["skills/own/good"]
