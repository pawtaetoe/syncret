import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from syncret.core import upstream

from .test_service import drain, remote, service  # noqa: F401 - fixtures

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
DEPLOY = "skills/upstream/acme/skills/deploy"


# commit a change to the fake upstream repo
def commit(repo, path, text, message="change"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    subprocess.run([*GIT, "add", "."], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, remote):  # noqa: F811 - pytest fixture
    return tmp_path / "github" / "acme" / "skills"


# add the upstream deploy skill and publish it
def publish_deploy(service, link="https://github.com/acme/skills"):  # noqa: F811
    service.add(link, skills=["deploy"])
    drain(service)
    proposal = service.proposals.list(states=["open"])[0]
    service.approve(proposal.id, proposal.base)
    drain(service)


def updates(service):  # noqa: F811
    return [p for p in service.proposals.list() if p.kind == "update"]


# a change to a tracked skill becomes one update proposal, vetted and scanned
def test_change_proposes_update(service, repo):  # noqa: F811
    publish_deploy(service)
    new = commit(repo, "skills/deploy/SKILL.md", "---\nname: deploy\ndescription: Handle deploy "
                 "tasks for the merge and scan tests here.\n---\n\n- Never deploy on Fridays.\n"
                 "- Always tag releases.\n")
    job = service.check_updates()
    drain(service)
    result = service.queue.get(job).result
    [update] = updates(service)
    assert result == {"proposals": [update.id], "checked": 1, "problems": []}
    assert update.state == "open"
    entry = service.repo.skills(f"proposal/{update.id}")["skills"][DEPLOY]
    assert (entry["pin"], entry["ref"], entry["targets"]) == (new, "HEAD", ["local"])
    _, diff = service.show(update.id)
    assert "+- Always tag releases." in diff
    service.check_updates()
    drain(service)
    assert len(updates(service)) == 1


# commits that leave a tracked skill's content alone propose nothing
def test_no_change_no_update(service, repo):  # noqa: F811
    publish_deploy(service)
    commit(repo, "README.md", "readme\n")
    commit(repo, "skills/good/SKILL.md", "---\nname: good\ndescription: Something else that "
           "changed in the repo.\n---\nBody.\n")
    job = service.check_updates()
    drain(service)
    assert service.queue.get(job).result["proposals"] == []
    assert updates(service) == []


# a skill added from a branch link follows that branch
def test_tracks_branch(service, repo):  # noqa: F811
    publish_deploy(service, "https://github.com/acme/skills/tree/main/skills/deploy")
    commit(repo, "skills/deploy/SKILL.md", "---\nname: deploy\ndescription: Handle deploy "
           "tasks for the merge and scan tests here.\n---\n\n- Never deploy at night.\n")
    service.check_updates()
    drain(service)
    [update] = updates(service)
    entry = service.repo.skills(f"proposal/{update.id}")["skills"][DEPLOY]
    assert entry["ref"] == "main"


# an unreachable upstream or a skill gone from it is reported, not fatal
def test_problems_reported(service, repo, tmp_path, monkeypatch):  # noqa: F811
    publish_deploy(service)
    subprocess.run(["git", "rm", "-q", "-r", "skills/deploy"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "drop deploy"], cwd=repo, check=True)
    job = service.check_updates()
    drain(service)
    found = service.queue.get(job)
    assert found.state == "done"
    assert "no longer at skills/deploy" in found.result["problems"][0]
    monkeypatch.setattr(upstream, "GITHUB", str(tmp_path / "nowhere" / "{owner}" / "{repo}"))
    job = service.check_updates()
    drain(service)
    assert service.queue.get(job).result["problems"][0].startswith("acme/skills@HEAD:")


# a check is queued when none ran within the interval, and never two at once
def test_schedule(service):  # noqa: F811
    assert service.schedule_due()
    assert not service.schedule_due()
    drain(service)
    now = datetime.now(UTC)
    assert not service.schedule_due(at=now + timedelta(hours=5))
    assert service.schedule_due(at=now + timedelta(hours=7))
    service.settings.save(check_interval_hours=1)
    drain(service)
    assert not service.schedule_due(at=now + timedelta(minutes=30))


# the whole loop: an upstream change, its update approved, then the merge using it rebuilt
def test_upstream_change_rebuilds_merge(service, repo, tmp_path):  # noqa: F811
    from .test_service import fake_llm, make_skill

    fake_llm(service, {
        "review": {"concerns": []},
        "overlap": {"label": "independent", "summary": "x", "disagreements": []},
        "merge": {"description": "Deploy safely and keep notes for the whole team.",
                  "body": "- Never deploy on Fridays.\n- Always take notes.\n", "dropped": []}
    })
    publish_deploy(service)
    service.add(str(make_skill(tmp_path / "own" / "notes", "notes", "- Always take notes.")))
    drain(service)
    proposal = service.proposals.list(states=["open"])[0]
    service.approve(proposal.id, proposal.base)
    drain(service)
    service.propose_merge([DEPLOY, "skills/own/notes"], "deploy-notes")
    drain(service)
    merge = next(p for p in service.proposals.list(states=["open"]) if p.kind == "merge")
    service.approve(merge.id, merge.base)
    drain(service)
    commit(repo, "skills/deploy/SKILL.md", "---\nname: deploy\ndescription: Handle deploy "
           "tasks for the merge and scan tests here.\n---\n\n- Never deploy on Fridays.\n"
           "Check the dashboard first.\n")
    service.check_updates()
    drain(service)
    [update] = updates(service)
    job = service.approve(update.id, update.base)
    drain(service)
    assert len(service.queue.get(job).result["rebuilds"]) == 1
    rebuilt = next(p for p in service.proposals.list(states=["open"]) if p.kind == "merge")
    assert rebuilt.title == "merge deploy-notes"
    assert rebuilt.id > update.id
