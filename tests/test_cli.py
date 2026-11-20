import pytest

from syncret.cli import main


# help lists both part 1 commands
def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "vet" in out
    assert "scan" in out
    assert "llm" in out


# each command exposes its spec flags
@pytest.mark.parametrize(
    ("command", "flags"),
    [
        ("vet", ["path", "--json"]),
        ("scan", ["dirs", "--json", "--no-llm"])
    ]
)
def test_command_flags(capsys, command, flags):
    with pytest.raises(SystemExit):
        main([command, "--help"])
    out = capsys.readouterr().out
    for flag in flags:
        assert flag in out


# no command prints usage and fails
def test_no_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2
