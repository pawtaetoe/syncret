import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from syncret.core.scan import scan
from syncret.core.skill import find_skills
from syncret.core.upstream import Upstream, UpstreamError
from syncret.core.vet import verdict, vet

_WIDTH = 120


# argument parser for every syncret command
def _parser():
    parser = argparse.ArgumentParser(
        prog="syncret",
        description="Vet agent skills and find duplicates and conflicts between them"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    vet = commands.add_parser("vet", help="check a skill for risky content, exit 1 on block")
    vet.add_argument("path", help="a skill, a folder of skills, or a link")
    vet.add_argument("--json", action="store_true", help="print the report as JSON")

    scan = commands.add_parser("scan", help="find duplicates and conflicts across skill folders")
    scan.add_argument("dirs", nargs="+", help="one or more skill folders")
    scan.add_argument("--json", action="store_true", help="print the report as JSON")
    scan.add_argument("--no-llm", action="store_true", help="run only checks that need no model")
    return parser


# vet reports for a local path or a github or skills.sh link; raises UpstreamError when not found
def _vet_reports(target):
    path = Path(target)
    if path.exists():
        return [vet(skill) for skill in find_skills(path)]
    upstream = Upstream.parse(target)
    if upstream:
        return [replace(vet(skill), path=link) for skill, link in upstream.load()]
    if "://" in target:
        raise UpstreamError(f"unsupported link: {target} (use a GitHub or skills.sh link)")
    raise UpstreamError(f"no such path: {target}")


# vet every skill at a path or link, exit 1 if any is blocked, 2 if there is nothing to vet
def _vet(args):
    try:
        reports = _vet_reports(args.path)
    except UpstreamError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if not reports:
        print(f"syncret: no SKILL.md found under {args.path}", file=sys.stderr)
        return 2
    overall = verdict(report.verdict for report in reports)
    if args.json:
        skills = [report.to_dict() for report in reports]
        print(json.dumps({"verdict": overall, "skills": skills}, indent=2))
    else:
        _print_reports(reports)
        if len(reports) > 1:
            print(f"\n{_summary(reports)}")
    return 1 if overall == "block" else 0


# one header line per report and one line per finding
def _print_reports(reports):
    for report in reports:
        print(f"{report.verdict:<6} {report.name}  {report.path}")
        for f in report.findings:
            text = f.text.strip()
            text = text if len(text) <= _WIDTH else text[:_WIDTH - 3] + "..."
            print(f"  {f.severity:<6} {f.rule}  {f.file}:{f.line}  {text}")


# count of skills per verdict, worst first
def _summary(reports):
    counts = Counter(report.verdict for report in reports)
    summary = ", ".join(f"{counts[v]} {v}" for v in ("block", "warn", "pass") if counts[v])
    return f"{len(reports)} skills: {summary}"


# a titled list of path groups, each led by its key
def _print_groups(title, groups):
    print(f"\n{title}: {len(groups) or 'none'}")
    for key, paths in groups:
        print(f"  {key}  {paths[0]}")
        for path in paths[1:]:
            print(f"  {' ' * len(key)}  {path}")


# vet and compare every skill under dirs; exit 1 if any is blocked, 2 if there are none
def _scan(args):
    missing = [d for d in args.dirs if not Path(d).is_dir()]
    if missing:
        print(f"syncret: no such folder: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not args.no_llm:
        print("syncret: no LLM configured, running only checks that need no model",
              file=sys.stderr)
    report = scan(args.dirs)
    if not report.skills:
        print(f"syncret: no SKILL.md found under {', '.join(args.dirs)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        flagged = [r for r in report.skills if r.verdict != "pass"]
        _print_reports(flagged)
        if flagged:
            print()
        print(f"vet: {_summary(report.skills)}")
        _print_groups("exact duplicates", [(d.hash[:12], d.paths) for d in report.duplicates])
        _print_groups("name collisions", [(c.name, c.paths) for c in report.collisions])
        _print_groups("overlap candidates, unconfirmed",
                      [(f"{o.score:.2f}", o.paths) for o in report.overlaps])
    return 1 if report.verdict == "block" else 0


# entry point, returns the process exit code
def main(argv=None):
    args = _parser().parse_args(argv)
    return _vet(args) if args.command == "vet" else _scan(args)
