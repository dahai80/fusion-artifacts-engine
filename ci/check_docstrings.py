#!/usr/bin/env python3
# Repo convention: docstrings are banned - use # comments only.
# Scans all tracked .py files (git ls-files) for module/class/function
# docstrings and exits 1 with file:line locations when any are found.
import ast
import subprocess
import sys

SKIP_FILES = set()  # generated/vendored file names to skip


def main() -> int:
    try:
        raw = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            check=True, capture_output=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"check_docstrings: cannot list tracked files: {exc}", file=sys.stderr)
        return 2
    files = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    bad = []
    checked = 0
    for path in files:
        if path.rsplit("/", 1)[-1] in SKIP_FILES:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"check_docstrings: skip unreadable file {path}: {exc}", file=sys.stderr)
            continue
        checked += 1
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                kind = getattr(node, "name", "<module>")
                bad.append(f"{path}:{body[0].lineno} {kind}")
    if bad:
        print(f"docstrings found ({len(bad)}) - repo convention forbids them, use # comments:")
        for item in bad:
            print(f"  {item}")
        return 1
    print(f"ok: no docstrings in {checked} tracked .py files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
