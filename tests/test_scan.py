import json
import shutil
from pathlib import Path

import pytest

from syncret.cli import main
from syncret.core.scan import scan, similarity

FIXTURES = Path(__file__).parent / "fixtures"
SCAN_DIRS = [str(FIXTURES / "scan-a"), str(FIXTURES / "scan-b")]
SKILLS = FIXTURES / "skills"


# every file under root with its bytes, symlinks recorded as their target
def _snapshot(root):
    state = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            state[str(path)] = ("link", str(path.readlink()))
        elif path.is_file():
            state[str(path)] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return state


# copies and symlinked copies across two dirs are one duplicate group
def test_duplicates():
    [duplicate] = scan(SCAN_DIRS).duplicates
    assert sorted(Path(p).relative_to(FIXTURES).as_posix() for p in duplicate.paths) == [
        "scan-a/billing-csv", "scan-b/billing-csv", "scan-b/billing-csv-link"
    ]


# same name with different content is a collision; same name and content is not
def test_collisions():
    [collision] = scan(SCAN_DIRS).collisions
    assert collision.name == "photo-rename"
    assert len(collision.paths) == 2


# reworded descriptions of one job are paired once, unconfirmed; unrelated ones are not
def test_overlaps():
    pairs = {
        tuple(sorted(Path(p).name for p in overlap.paths)): overlap.label
        for overlap in scan(SCAN_DIRS).overlaps
    }
    assert pairs == {
        ("billing-csv", "csv-summary"): "unconfirmed",
        ("photo-rename", "photo-rename"): "unconfirmed"
    }


@pytest.mark.parametrize(
    ("a", "b", "paired"),
    [
        ("Review pull requests for bugs and style problems before merging.",
         "Check a pull request for bugs and style issues prior to merge.", True),
        ("Rename photos by the date they were taken.",
         "Rename image files using the EXIF date they were shot.", True),
        ("Format CSV exports from the billing system into monthly summary tables.",
         "Rename photos by the date they were taken.", False),
        ("Use this skill whenever the user wants a Word document.",
         "Use this skill whenever the user wants a slide deck.", False)
    ]
)
def test_similarity(a, b, paired):
    assert (similarity(a, b) >= 0.3) is paired


# a path given twice, or nested in another given dir, is scanned once
def test_paths_deduped():
    report = scan([*SCAN_DIRS, SCAN_DIRS[0], str(FIXTURES / "scan-a" / "billing-csv")])
    assert len(report.skills) == 6


# scan never writes to the folders it reads
def test_read_only(capsys):
    before = {d: _snapshot(d) for d in SCAN_DIRS}
    main(["scan", *SCAN_DIRS, "--no-llm"])
    main(["scan", *SCAN_DIRS, "--json"])
    assert {d: _snapshot(d) for d in SCAN_DIRS} == before


# cli: duplicates, collisions and overlaps alone exit 0
def test_cli_report(capsys):
    assert main(["scan", *SCAN_DIRS, "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "vet: 6 skills: 6 pass" in out
    assert "exact duplicates: 1" in out
    assert "name collisions: 1" in out
    assert "overlap candidates, unconfirmed: 2" in out


# cli: every skill is vetted and a blocked one exits 1
def test_cli_block_exit(tmp_path, capsys):
    shutil.copytree(SKILLS / "remote-run-bad", tmp_path / "remote-run-bad")
    assert main(["scan", *SCAN_DIRS, str(tmp_path), "--no-llm"]) == 1
    assert "block  remote-run-bad" in capsys.readouterr().out


# cli: without --no-llm it runs the same checks and says no llm is configured
def test_cli_no_llm_notice(capsys):
    assert main(["scan", *SCAN_DIRS]) == 0
    captured = capsys.readouterr()
    assert "no LLM configured" in captured.err
    assert "exact duplicates: 1" in captured.out


# cli: json has the verdict, vet reports and every group
def test_cli_json(capsys):
    assert main(["scan", *SCAN_DIRS, "--json", "--no-llm"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"verdict", "skills", "duplicates", "collisions", "overlaps", "notes"}
    assert data["verdict"] == "pass"
    assert len(data["skills"]) == 6
    assert data["collisions"][0]["name"] == "photo-rename"
    assert data["overlaps"][0]["label"] == "unconfirmed"


# cli: a missing folder or no skills at all exits 2
def test_cli_nothing_to_scan(tmp_path, capsys):
    assert main(["scan", str(tmp_path / "missing"), "--no-llm"]) == 2
    assert main(["scan", str(tmp_path), "--no-llm"]) == 2
    assert "no SKILL.md found" in capsys.readouterr().err
