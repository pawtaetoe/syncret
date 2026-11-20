import argparse
import getpass
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from syncret.core.diff_rules import diff_rules, skill_file
from syncret.core.llm import PROVIDERS, Llm, LlmError, default_config_path
from syncret.core.merge import MergeError, Source, merge
from syncret.core.review import reviewed
from syncret.core.scan import scan
from syncret.core.skill import Skill, find_skills
from syncret.core.text import shown
from syncret.core.upstream import Upstream, UpstreamError
from syncret.core.vet import verdict, vet

_WIDTH = 120


# options for commands that can use an llm
def _llm_options(command):
    command.add_argument("--no-llm", action="store_true", help="run only checks that need no model")
    command.add_argument("--config", help=f"LLM config file (default {default_config_path()})")
    command.add_argument("--public", action="store_true",
                         help="treat local skills as public, so providers without can_see_own "
                              "may see them")


# the configured llm, or None with --no-llm or without a config; LlmError on a broken config
def _llm(args):
    return None if args.no_llm else Llm.load(args.config)


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
    _llm_options(vet)

    scan = commands.add_parser(
        "scan", help="vet skill folders and find duplicates, name collisions and overlaps"
    )
    scan.add_argument("dirs", nargs="+", help="one or more skill folders")
    scan.add_argument("--json", action="store_true", help="print the report as JSON")
    _llm_options(scan)

    diff = commands.add_parser(
        "diff-rules", help="list rules kept, changed or dropped between two versions of a skill"
    )
    diff.add_argument("old", help="the earlier skill folder or SKILL.md")
    diff.add_argument("new", help="the later skill folder or SKILL.md")
    diff.add_argument("--json", action="store_true", help="print the report as JSON")

    merge_cmd = commands.add_parser(
        "merge", help="merge skills into one with an LLM, keeping every rule or saying why not"
    )
    merge_cmd.add_argument("sources", nargs="+", help="two or more skill folders or links")
    merge_cmd.add_argument("--name", required=True, help="name of the merged skill")
    merge_cmd.add_argument("--out", required=True, help="folder to create for the merged skill")
    merge_cmd.add_argument("--json", action="store_true", help="print the report as JSON")
    merge_cmd.add_argument("--config", help=f"LLM config file (default {default_config_path()})")
    merge_cmd.add_argument("--public", action="store_true",
                           help="treat local skills as public, so providers without can_see_own "
                                "may see them")

    serve = commands.add_parser("serve", help="run the server: dashboard, MCP and releases")
    serve.add_argument("--data", help="data folder (default $SYNCRET_DATA or "
                                      "~/.local/share/syncret/server)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on")
    serve.add_argument("--port", type=int, default=8080, help="port to listen on")
    password = commands.add_parser("set-password", help="set the dashboard password")
    password.add_argument("--data", help="data folder (as for serve)")
    token = commands.add_parser("set-claude-token",
                                help="save a `claude setup-token` token for the server")
    token.add_argument("--data", help="data folder (as for serve)")

    llm = commands.add_parser("llm", help="check the configured LLM providers")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    check = llm_commands.add_parser(
        "check", help="ask each provider a test question that holds no skill content"
    )
    check.add_argument("--config", help=f"config file (default {default_config_path()})")
    check.add_argument("--data", help="check a server's providers, with its saved Claude token "
                                      "(default $SYNCRET_DATA when set)")
    return parser


# (skill, report, own) for a local path or a github or skills.sh link; local skills are own
# unless public; raises UpstreamError when not found
def _vet_reports(target, public):
    path = Path(target)
    if path.exists():
        return [(skill, vet(skill), not public) for skill in find_skills(path)]
    upstream = Upstream.parse(target)
    if upstream:
        return [(skill, replace(vet(skill), path=link), False) for skill, link in upstream.load()]
    if "://" in target:
        raise UpstreamError(f"unsupported link: {target} (use a GitHub or skills.sh link)")
    raise UpstreamError(f"no such path: {target}")


# vet every skill at a path or link, exit 1 if any is blocked, 2 if there is nothing to vet
def _vet(args):
    try:
        llm = _llm(args)
        vetted = _vet_reports(args.path, args.public)
    except (UpstreamError, LlmError) as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if not vetted:
        print(f"syncret: no SKILL.md found under {args.path}", file=sys.stderr)
        return 2
    reports, notes = [], []
    for skill, report, own in vetted:
        if llm is not None:
            report, note = reviewed(skill, report, llm, own)
            notes.append(note)
        reports.append(report)
    overall = verdict(report.verdict for report in reports)
    if args.json:
        skills = [report.to_dict() for report in reports]
        print(json.dumps({"verdict": overall, "skills": skills, "notes": notes}, indent=2))
    else:
        _print_notes(notes)
        _print_reports(reports)
        if len(reports) > 1:
            print(f"\n{_summary(reports)}")
    return 1 if overall == "block" else 0


# notes on how the llm steps went, to stderr so they never mix with the report
def _print_notes(notes):
    for note in notes:
        print(f"syncret: {_shown(note)}", file=sys.stderr)


_shown = shown


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
    try:
        llm = _llm(args)
    except LlmError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if llm is None and not args.no_llm:
        print("syncret: no LLM configured, running only checks that need no model",
              file=sys.stderr)
    report = scan(args.dirs, llm, own=not args.public)
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
        if llm is None:
            _print_groups("overlap candidates, unconfirmed",
                          [(f"{o.score:.2f}", o.paths) for o in report.overlaps])
        else:
            _print_overlaps(report.overlaps)
        _print_notes(report.notes)
    return 1 if report.verdict == "block" else 0


# labelled overlaps: label, score and paths, then the summary and each quoted disagreement
def _print_overlaps(overlaps):
    print(f"\noverlaps: {len(overlaps) or 'none'}")
    for o in overlaps:
        key = f"{o.label} {o.score:.2f}"
        print(f"  {key}  {_shown(o.paths[0])}")
        print(f"  {' ' * len(key)}  {_shown(o.paths[1])}")
        detail = o.summary or o.note
        if detail:
            print(f"      {_shown(detail)}")
        for d in o.disagreements:
            for side in ("a", "b"):
                quote = d[side]
                print(f"      {side.upper()} {_shown(quote['file'])}:{quote['line']}  "
                      f"{_shown(quote['text'])[:_WIDTH]}")
            print(f"        {_shown(d['note'])}")


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


# one merge source from a local skill folder or a link to one skill
def _source(target):
    path = Path(target)
    if path.exists():
        if not (path / "SKILL.md").is_file():
            raise UpstreamError(f"{target} is not a skill folder (no SKILL.md)")
        return Source(Skill(path), str(path.resolve()))
    upstream = Upstream.parse(target)
    if upstream is None:
        raise UpstreamError(f"no such skill or supported link: {target}")
    loaded = upstream.load()
    if len(loaded) != 1:
        raise UpstreamError(f"{target} holds {len(loaded)} skills; link to exactly one")
    skill, link = loaded[0]
    return Source(skill, link, commit=link.split("/tree/")[1].split("/")[0])


# merge skills; exit 1 if rules went missing without a reason or the result is blocked, 2 if
# it could not run
def _merge(args):
    try:
        llm = Llm.load(args.config)
        sources = [_source(target) for target in args.sources]
    except (UpstreamError, LlmError) as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if llm is None:
        config = args.config or default_config_path()
        print(f"syncret: merge needs an LLM; none configured ({config})", file=sys.stderr)
        return 2
    own = not args.public and any(not s.commit for s in sources)
    try:
        merged = merge(sources, args.name, args.out, llm, own)
    except MergeError as error:
        print(f"syncret: {error}", file=sys.stderr)
        for rule in error.unexplained:
            print(f"  {_shown(rule)}", file=sys.stderr)
        return 1 if error.unexplained else 2
    report = vet(Skill(merged.out))
    if args.json:
        print(json.dumps({"out": str(merged.out), "provider": merged.provider,
                          "dropped": merged.dropped, "untraced": merged.untraced,
                          "vet": report.to_dict()}, indent=2))
    else:
        print(f"merged {len(sources)} skills into {_shown(merged.out)} (by {merged.provider})")
        print(f"dropped with a reason: {len(merged.dropped)}")
        for d in merged.dropped:
            print(f"  {_shown(d['source'])}: {_shown(d['rule'])}")
            print(f"    {_shown(d['reason'])}")
        print(f"in no source, check these: {len(merged.untraced)}")
        for u in merged.untraced:
            print(f"  SKILL.md:{u['line']}  {_shown(u['text'])[:_WIDTH]}")
        _print_reports([report])
    return 1 if report.verdict == "block" else 0


# the server's data folder: --data, then $SYNCRET_DATA, then a folder in the user's home
def _data(args):
    default = Path.home() / ".local" / "share" / "syncret" / "server"
    return Path(args.data or os.environ.get("SYNCRET_DATA") or default)


# run the dashboard, MCP, release endpoints and the worker in one process
def _serve(args):
    try:
        import uvicorn

        from syncret.server.app import create_app
    except ImportError:
        print('syncret: the server needs `pip install "syncret[server]"`', file=sys.stderr)
        return 2
    uvicorn.run(create_app(_data(args)), host=args.host, port=args.port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


# ask for the dashboard password twice and store its scrypt hash in the data folder
def _set_password(args):
    from syncret.server.auth import set_password

    first = getpass.getpass("New dashboard password: ")
    if first != getpass.getpass("Again: "):
        print("syncret: the passwords differ", file=sys.stderr)
        return 2
    try:
        set_password(_data(args), first)
    except ValueError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    print(f"password set for {_data(args)}")
    return 0


# read a claude setup-token token at a silent prompt and store it in the data folder
def _set_claude_token(args):
    from syncret.server.settings import Settings

    try:
        Settings(_data(args)).save_token(getpass.getpass("Token from `claude setup-token`: "))
    except ValueError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    print(f"Claude token saved for {_data(args)}; it lasts a year")
    return 0


# the providers to check and where they came from: --config, else the server's own providers
# (with its saved claude token) when run beside a server data folder, else the user config
def _check_chain(args):
    data = args.data or os.environ.get("SYNCRET_DATA")
    if args.config is None and data and (Path(data) / "llm.toml").is_file():
        from syncret.server.service import Service

        return Service(data).llm(), f"server providers in {data}"
    return Llm.load(args.config), args.config or default_config_path()


# one line per provider: works or not, its type and model, and whether it may see own skills;
# exit 0 if any works, 1 if none do, 2 without a config
def _llm_check(args):
    try:
        chain, where = _check_chain(args)
    except LlmError as error:
        print(f"syncret: {error}", file=sys.stderr)
        return 2
    if chain is None:
        print(f"syncret: no LLM configured ({where})", file=sys.stderr)
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
    commands = {"vet": _vet, "scan": _scan, "diff-rules": _diff_rules, "merge": _merge,
                "llm": _llm_check, "serve": _serve, "set-password": _set_password,
                "set-claude-token": _set_claude_token}
    return commands[args.command](args)
