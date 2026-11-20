import json

import pytest

from syncret.cli import main
from syncret.core.diff_rules import diff_rules, extract_rules

HEAD = "---\nname: deploy\ndescription: Always use this to deploy the app safely.\n---\n\n"
OLD = HEAD + """# Deploy

- Always run the full test suite before deploying.
- Never push directly to main.
- Only deploy from a tagged commit. Do not deploy on Fridays.

```bash
# never runs, this is code
```

Use `make deploy` for the actual release.
"""
NEW = HEAD + """# Deploy

- **Always** run the full test suite before deploying.
- Always push directly to main.
- Only deploy from a signed, tagged commit.
- Must notify #releases when done.
"""


# rules are sentences with rule words; frontmatter, code blocks and plain prose are skipped
def test_extract_rules():
    rules = extract_rules(OLD)
    assert [(r.line, r.text) for r in rules] == [
        (8, "- Always run the full test suite before deploying."),
        (9, "- Never push directly to main."),
        (10, "- Only deploy from a tagged commit."),
        (10, "Do not deploy on Fridays.")
    ]


# kept, changed, dropped and added, with a flipped meaning caught
def test_diff():
    result = diff_rules(OLD, NEW)
    assert [p.old.line for p in result.kept] == [8]
    changed = {p.old.text: (p.new.text, p.flipped) for p in result.changed}
    assert changed == {
        "- Never push directly to main.": ("- Always push directly to main.", True),
        "- Only deploy from a tagged commit.": (
            "- Only deploy from a signed, tagged commit.", False
        )
    }
    assert [r.text for r in result.dropped] == ["Do not deploy on Fridays."]
    assert [r.text for r in result.added] == ["- Must notify #releases when done."]
    assert not result.ok


# the same rules reordered and restyled are all kept
def test_unchanged_is_ok():
    tests = "Always run the full test suite before deploying."
    main_branch = "Never push directly to main."
    restyled = HEAD + f"1. {main_branch}\n\n* **{tests}**\n"
    original = HEAD + f"- {tests}\n- {main_branch}\n"
    result = diff_rules(original, restyled)
    assert len(result.kept) == 2
    assert result.ok


# the spec's check: a merge that drops a rule is caught
def test_dropped_rule_caught():
    source = HEAD + "- Never commit secrets.\n- Always sign commits.\n"
    merged = HEAD + "- Always sign commits.\n"
    result = diff_rules(source, merged)
    assert [r.text for r in result.dropped] == ["- Never commit secrets."]
    assert not result.ok


# a curly apostrophe counts like a straight one, and the negation is seen
def test_curly_apostrophe():
    curly = HEAD + f"- Don{chr(0x2019)}t force-push shared branches.\n"
    straight = HEAD + "- Do force-push shared branches.\n"
    [rule] = extract_rules(curly)
    assert rule.negative
    result = diff_rules(curly, straight)
    assert result.dropped or any(p.flipped for p in result.changed)


# each rule is matched once, so a duplicated rule cannot hide a dropped one
def test_one_to_one():
    old = HEAD + "- Never skip review.\n- Never skip tests.\n"
    new = HEAD + "- Never skip review.\n- Never skip review.\n"
    result = diff_rules(old, new)
    assert len(result.kept) == 1
    assert len(result.changed) + len(result.dropped) == 1
    assert [r.text for r in result.added] in ([], ["- Never skip review."])


# cli: folders or files, exit 1 on a dropped or flipped rule, 0 when every rule is kept
def test_cli(tmp_path, capsys):
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "SKILL.md").write_text(OLD)
    (tmp_path / "new.md").write_text(NEW)
    assert main(["diff-rules", str(tmp_path / "old"), str(tmp_path / "new.md")]) == 1
    out = capsys.readouterr().out
    assert "dropped: 1\n  old:10  Do not deploy on Fridays." in out
    assert "old:9  - Never push directly to main.  meaning flipped" in out
    assert main(["diff-rules", str(tmp_path / "old"), str(tmp_path / "old")]) == 0


# cli: json report and a missing path
def test_cli_json_and_missing(tmp_path, capsys):
    (tmp_path / "a.md").write_text(OLD)
    (tmp_path / "b.md").write_text(NEW)
    assert main(["diff-rules", str(tmp_path / "a.md"), str(tmp_path / "b.md"), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"kept", "changed", "dropped", "added", "ok"}
    assert data["ok"] is False
    assert data["dropped"] == [{"line": 10, "text": "Do not deploy on Fridays."}]
    assert data["changed"][0]["flipped"] is True
    assert main(["diff-rules", str(tmp_path / "a.md"), str(tmp_path / "missing")]) == 2
    assert "no such skill or file" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("line", "is_rule"),
    [
        ("You must sign every commit.", True),
        ("Should not be used for secrets.", True),
        ("We can't ship without tests.", True),
        ("The build runs on every push.", False),
        ("Mustard is a condiment.", False)
    ]
)
def test_rule_words(line, is_rule):
    assert bool(extract_rules(HEAD + line + "\n")) is is_rule
