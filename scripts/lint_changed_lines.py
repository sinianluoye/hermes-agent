"""Run ruff or pylint and report only diagnostics on lines changed in a diff.

Used by the `ruff enforcement` and `pylint enforcement` blocking CI jobs to
implement the AGENTS.md "Code Quality Checks" policy: new/refactored code must
pass, existing code is grandfathered.  Touching a single line of a legacy file
should NOT make every pre-existing violation in that file fail CI — only the
lines actually added/modified by the PR are evaluated.

Usage:
    python scripts/lint_changed_lines.py --tool ruff   --base <sha> [files...]
    python scripts/lint_changed_lines.py --tool pylint --base <sha> [files...]

If no files are passed, all changed .py files vs <base> are scanned.
Exits non-zero if any violation falls on a changed line.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "-z", "--name-only", "--diff-filter=ACMR", base, "HEAD", "--", "*.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def changed_lines(base: str, path: str) -> set[int]:
    """Return the set of new-side line numbers that are added/modified in `path`."""
    out = subprocess.run(
        ["git", "diff", "-U0", "--no-color", base, "HEAD", "--", path],
        check=True, capture_output=True, text=True,
    ).stdout
    lines: set[int] = set()
    cur = 0
    for line in out.splitlines():
        m = HUNK_HEADER.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            cur = start
            # New file lines [start, start+count) are modified.
            for i in range(count):
                lines.add(start + i)
            continue
        # Lines starting with '+' (but not '+++') are added in the new file;
        # they're already accounted for by the hunk header on -U0.  Nothing to do.
    return lines


def run_ruff(files: list[str]) -> list[dict]:
    if not files:
        return []
    proc = subprocess.run(
        ["ruff", "check", "--output-format=json", "--exit-zero", *files],
        check=False, capture_output=True, text=True,
    )
    try:
        return json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        sys.stderr.write(f"ruff produced non-JSON output:\n{proc.stdout}\n{proc.stderr}\n")
        sys.exit(2)


def run_pylint(files: list[str]) -> list[dict]:
    if not files:
        return []
    proc = subprocess.run(
        ["pylint", "--output-format=json", "--disable=import-error,no-name-in-module", *files],
        check=False, capture_output=True, text=True,
    )
    # pylint exits non-zero on findings; output is still valid JSON.
    if not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"pylint produced non-JSON output:\n{proc.stdout}\n{proc.stderr}\n")
        sys.exit(2)


def filter_ruff(diags: list[dict], allowed: dict[str, set[int]]) -> list[dict]:
    out = []
    for d in diags:
        path = d.get("filename", "")
        # ruff emits absolute paths; relativize.
        try:
            path = str(Path(path).resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            pass
        line = (d.get("location") or {}).get("row")
        if line is None:
            continue
        if line in allowed.get(path, set()):
            d["_path"] = path
            d["_line"] = line
            out.append(d)
    return out


def filter_pylint(diags: list[dict], allowed: dict[str, set[int]]) -> list[dict]:
    out = []
    for d in diags:
        path = d.get("path", "")
        try:
            path = str(Path(path).resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            pass
        line = d.get("line")
        if line is None:
            continue
        if line in allowed.get(path, set()):
            d["_path"] = path
            d["_line"] = line
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", choices=["ruff", "pylint"], required=True)
    ap.add_argument("--base", required=True, help="base git SHA / ref to diff against")
    ap.add_argument("files", nargs="*", help="explicit file list; default: all changed .py files")
    args = ap.parse_args()

    files = args.files or changed_files(args.base)
    if not files:
        print(f"[{args.tool}] no changed .py files; nothing to check.")
        return 0

    allowed: dict[str, set[int]] = {f: changed_lines(args.base, f) for f in files}

    if args.tool == "ruff":
        diags = filter_ruff(run_ruff(files), allowed)
        for d in diags:
            print(f"{d['_path']}:{d['_line']}:{(d.get('location') or {}).get('column','')}: "
                  f"{d.get('code','?')} {d.get('message','')}")
    else:
        diags = filter_pylint(run_pylint(files), allowed)
        for d in diags:
            print(f"{d['_path']}:{d['_line']}:{d.get('column','')}: "
                  f"{d.get('message-id','?')} ({d.get('symbol','?')}) {d.get('message','')}")

    if diags:
        print(f"\n[{args.tool}] {len(diags)} violation(s) on changed lines.", file=sys.stderr)
        return 1
    print(f"[{args.tool}] clean on changed lines across {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
