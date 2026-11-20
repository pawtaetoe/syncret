from .test_service import drain, fake_llm, make_skill, service  # noqa: F401 - service is a fixture

RULES = "- Never commit secrets.\n- Always sign commits.\n- Never push to main."


def approve_open(service, kind):  # noqa: F811
    proposal = next(p for p in service.proposals.list(states=["open"]) if p.kind == kind)
    job = service.approve(proposal.id, proposal.base)
    drain(service)
    return proposal, service.queue.get(job).result


# an approved change to a merge source rebuilds the merge once; the rebuilt merge does not loop
def test_rebuild(service, tmp_path):  # noqa: F811
    fake_llm(service, {
        "review": {"concerns": []},
        "overlap": {"label": "independent", "summary": "Different jobs.", "disagreements": []},
        "merge": {"description": "Keep git history clean and safe for the whole team.",
                  "body": RULES + "\n", "dropped": []}
    })
    commits = make_skill(tmp_path / "a" / "commits", "commits",
                         "- Never commit secrets.\n- Always sign commits.",
                         "Make safe git commits for the team repository.")
    pushes = make_skill(tmp_path / "b" / "pushes", "pushes", "- Never push to main.",
                        "Push branches the safe way for the shared repository.")
    for folder in (commits, pushes):
        service.add(str(folder))
        drain(service)
        approve_open(service, "add")
    service.propose_merge(["skills/own/commits", "skills/own/pushes"], "git-rules")
    drain(service)
    _, result = approve_open(service, "merge")
    assert result["rebuilds"] == []
    merged = service.repo.skills("published")["skills"]["skills/merged/git-rules"]
    assert merged["sources"] == ["skills/own/commits", "skills/own/pushes"]

    text = (commits / "SKILL.md").read_text()
    (commits / "SKILL.md").write_text(text + "Write clear messages.\n")
    service.add(str(commits))
    drain(service)
    update, result = approve_open(service, "update")
    assert len(result["rebuilds"]) == 1
    assert f"rebuilding skills/merged/git-rules as job {result['rebuilds'][0]}" in \
        service.proposals.get(update.id).notes
    rebuilt = next(p for p in service.proposals.list(states=["open"]) if p.kind == "merge")
    assert rebuilt.title == "merge git-rules"
    _, result = approve_open(service, "merge")
    assert result["rebuilds"] == []
    assert not [p for p in service.proposals.list(states=["open", "checking"])]

    service.remove("skills/own/pushes")
    drain(service)
    removal, result = approve_open(service, "remove")
    assert result["rebuilds"] == []
    assert any("uses a removed source" in n for n in service.proposals.get(removal.id).notes)


# without an llm, a changed source is noted instead of queuing a merge that would fail
def test_no_llm_noted(service, tmp_path):  # noqa: F811
    fake_llm(service, {"review": {"concerns": []}, "overlap": {"label": "independent",
                                                               "summary": "x",
                                                               "disagreements": []},
                       "merge": {"description": "Keep git history clean and safe for the team.",
                                 "body": RULES + "\n", "dropped": []}})
    commits = make_skill(tmp_path / "a" / "commits", "commits",
                         "- Never commit secrets.\n- Always sign commits.",
                         "Make safe git commits for the team repository.")
    pushes = make_skill(tmp_path / "b" / "pushes", "pushes", "- Never push to main.",
                        "Push branches the safe way for the shared repository.")
    for folder in (commits, pushes):
        service.add(str(folder))
        drain(service)
        approve_open(service, "add")
    service.propose_merge(["skills/own/commits", "skills/own/pushes"], "git-rules")
    drain(service)
    approve_open(service, "merge")
    service.settings.llm_path.unlink()
    (commits / "SKILL.md").write_text((commits / "SKILL.md").read_text() + "More.\n")
    service.add(str(commits))
    drain(service)
    update, result = approve_open(service, "update")
    assert result["rebuilds"] == []
    assert any("no LLM is configured" in n for n in service.proposals.get(update.id).notes)
