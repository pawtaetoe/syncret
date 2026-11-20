import argparse
import sys


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


# entry point, returns the process exit code
def main(argv=None):
    args = _parser().parse_args(argv)
    # shortcut: commands are stubs until tasks 3 and 9 wire them up
    print(f"syncret {args.command} is not implemented yet", file=sys.stderr)
    return 2
