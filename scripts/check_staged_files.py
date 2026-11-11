from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


def main() -> int:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        print(f"Unable to inspect staged files: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode

    blocked = [
        name
        for name in result.stdout.splitlines()
        if PurePosixPath(name).name.startswith(".env.")
        and PurePosixPath(name).name != ".env.example"
        or PurePosixPath(name).name == ".env"
    ]
    if blocked:
        print(
            "Refusing to commit environment files: " + ", ".join(sorted(blocked)),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
