import subprocess
import sys
from pathlib import Path


def _run(commands: list[list[str]]) -> int:
    project_root = next(
        (
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / "pyproject.toml").is_file()
        ),
        None,
    )
    if project_root is None:
        print("Could not find a project root containing pyproject.toml.", file=sys.stderr)
        return 1
    first_error = 0
    for command in commands:
        try:
            result = subprocess.run([sys.executable, "-m", *command], cwd=project_root, check=False)
            code = result.returncode
        except OSError:
            print("Could not start a development tool.", file=sys.stderr)
            code = 1
        if code and not first_error:
            first_error = code if code > 0 else 1
    return first_error


def check() -> int:
    return _run([["ruff", "format", "--check", "."], ["ruff", "check", "."], ["basedpyright"]])


def fix() -> int:
    _run([["ruff", "check", "--fix", "."], ["ruff", "format", "."]])
    return check()


def test() -> int:
    return _run([["pytest", *sys.argv[1:]]])
