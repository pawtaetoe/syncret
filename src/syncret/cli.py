import argparse
import json
import sys
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path

from syncret.core.diff_rules import diff_rules, skill_file
from syncret.core.llm import PROVIDERS, Llm, LlmError, default_config_path
from syncret.core.scan import scan
from syncret.core.skill import find_skills
from syncret.core.upstream import Upstream, UpstreamError
from syncret.core.vet import verdict, vet

_WIDTH = 120


# argument parser for every syncret command
def _parser():
    parser = argparse.ArgumentParser(
        prog="syncret",
        description="Vet agent skills for risky content and find duplicates and overlaps"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    vet = commands.add_parser("vet", help="check a skill for risky content, exit 1 on block")
    vet.add_argument("path", help="a skill, a folder of skills, or a link")
    vet.add_argument("--json", action="store_true", help="print the report as JSON")

    scan = commands.add_parser(
        "scan", help="vet skill folders and find duplicates, name collisions and overlaps"
    )
    scan.add_argument("dirs", nargs="+", help="one or more skill folders")
    scan.add_argument("--json", action="store_true", help="print the report as JSON")
    scan.add_argument("--no-llm", action="store_true", help="run only checks that need no model")

    diff = commands.add_parser(
        "diff-rules", help="list rules kept, changed or dropped between two versions of a skill"
    )
    diff.add_argument("old", help="the earlier skill folder or SKILL.md")
    diff.add_argument("new", help="the later skill folder or SKILL.md")
    diff.add_argument("--json", action="store_true", help="print the report as JSON")

    llm = commands.add_parser("llm", help="check the configured LLM providers")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    check = llm_commands.add_parser(
        "check", help="ask each provider a test question that holds no skill content"
    )
    check.add_argument("--config", help=f"config file (default {default_config_path()})")
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


# untrusted text made safe for a terminal: control, format and line-separator chars become
# visible <U+XXXX> markers, so a skill cannot forge report lines or send escape codes
def _shown(text):
    return "".join(
        f"<U+{ord(c):04X}>" if c != "\t" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
        else c
        for c in str(text)
    )


# one header line per report and one line per finding
def _print_reports(reports):
    for report in reports:
        print(f"{report.verdict:<6} {_shown(report.name)}  {_shown(report.path)}")
        for f in report.findings:
            text = _shown(f.text.strip())
            text = text if len(text) <= _WIDTH else text[:_WIDTH - 3] + "..."
            print(f"  {f.severity:<6} {f.rule}  {_shown(f.file)}:{f.line}  {text}")


# count of skills per verdict, worst first
def _summary(reports):
    counts = Counter(report.verdict for report in reports)
    summary = ", ".join(f"{counts[v]} {v}" for v in ("block", "warn", "pass") if counts[v])
    return f"{len(reports)} skills: {summary}"


# a titled list of path groups, each led by its key
def _print_groups(title, groups):
    print(f"\n{title}: {len(groups) or 'none'}")
    for key, paths in groups:
        key = _shown(key)
        print(f"  {key}  {_shown(paths[0])}")
        for path in paths[1:]:
            print(f"  {' ' * len(key)}  {_shown(path)}")


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


# one rule as "old:12  text", shortened and made safe for a terminal
def _rule(side, rule):
    text = _shown(rule.text)
    text = text if len(text) <= _WIDTH else text[:_WIDTH - 3] + "..."
    return f"{side}:{rule.line}  {text}"


# rules kept, changed, dropped or added; exit 1 if one is dropped or flips meaning, 2 if missing
def _diff_rules(args):
    old, new = skill_file(args.old), skill_file(args.new)
    missing = [str(p) for p in (old, new) if not p.is_file()]
    if missing:
        print(f"syncret: no such skill or file: {', '.join(missing)}", file=sys.stderr)
        return 2
    result = diff_rules(old.read_text(errors="replace"), new.read_text(errors="replace"))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.ok else 1
    print(f"kept: {len(result.kept)}")
    print(f"changed: {len(result.changed)}")
    for pair in result.changed:
        flag = "  meaning flipped" if pair.flipped else ""
        print(f"  {pair.score:.2f}  {_rule('old', pair.old)}{flag}")
        print(f"        {_rule('new', pair.new)}")
    print(f"dropped: {len(result.dropped)}")
    for rule in result.dropped:
        print(f"  {_rule('old', rule)}")
    print(f"added: {len(result.added)}")
    for rule in result.added:
        print(f"  {_rule('new', rule)}")
    return 0 if result.ok else 1


# one line per provider: works or not, its type and model, and whether it may see own skills;
# exit 0 if any works, 1 if none do, 2 without a config
def _llm_check(args):
    try:
        chain = Llm.load(args.config)
    except LlmError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if chain is None:
        print(f"syncret: no LLM configured ({args.config or default_config_path()})",
              file=sys.stderr)
        return 2
    kinds = {cls: kind for kind, cls in PROVIDERS.items()}
    working = 0
    for provider, error in chain.check():
        working += error is None
        model = getattr(provider, "model", "") or "default model"
        own = "may see own skills" if provider.can_see_own else "upstream skills only"
        status = "ok" if error is None else "fail"
        print(f"{status:<5} {provider.name}  {kinds[type(provider)]}, {model}, {own}")
        if error:
            print(f"      {_shown(error)}")
    return 0 if working else 1


# entry point, returns the process exit code
def main(argv=None):
    args = _parser().parse_args(argv)
    commands = {"vet": _vet, "scan": _scan, "diff-rules": _diff_rules, "llm": _llm_check}
    return commands[args.command](args)
