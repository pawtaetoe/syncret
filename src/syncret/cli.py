import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from syncret.core.skill import find_skills
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


# vet every skill at path, exit 1 if any is blocked, 2 if there is nothing to vet
def _vet(args):
    path = Path(args.path)
    if not path.exists():
        print(f"syncret: no such path: {path}", file=sys.stderr)
        return 2
    reports = [vet(skill) for skill in find_skills(path)]
    if not reports:
        print(f"syncret: no SKILL.md found under {path}", file=sys.stderr)
        return 2
    overall = verdict(report.verdict for report in reports)
    if args.json:
        skills = [report.to_dict() for report in reports]
        print(json.dumps({"verdict": overall, "skills": skills}, indent=2))
    else:
        for report in reports:
            print(f"{report.verdict:<6} {report.name}  {report.path}")
            for f in report.findings:
                text = f.text.strip()
                text = text if len(text) <= _WIDTH else text[:_WIDTH - 3] + "..."
                print(f"  {f.severity:<6} {f.rule}  {f.file}:{f.line}  {text}")
        if len(reports) > 1:
            counts = Counter(report.verdict for report in reports)
            summary = ", ".join(f"{counts[v]} {v}" for v in ("block", "warn", "pass") if counts[v])
            print(f"\n{len(reports)} skills: {summary}")
    return 1 if overall == "block" else 0


# entry point, returns the process exit code
def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "vet":
        return _vet(args)
    # shortcut: scan is a stub until task 9 wires it up
    print(f"syncret {args.command} is not implemented yet", file=sys.stderr)
    return 2
