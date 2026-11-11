from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "pyright-baseline.json"


class Diagnostic(TypedDict):
    file: str
    message: str
    rule: str | None
    severity: str


def _diagnostic_key(diagnostic: Diagnostic) -> str:
    file_path = Path(diagnostic["file"]).resolve().relative_to(ROOT).as_posix()
    return json.dumps(
        [
            file_path,
            diagnostic["severity"],
            diagnostic["rule"],
            diagnostic["message"],
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _run_pyright() -> tuple[list[Diagnostic], int]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyright", "--outputjson"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Pyright is not installed; install requirements-dev.txt.") from exc

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Pyright did not return valid JSON: {result.stderr}") from exc

    diagnostics = report.get("generalDiagnostics")
    if not isinstance(diagnostics, list):
        raise RuntimeError("Pyright output is missing generalDiagnostics.")
    return cast(list[Diagnostic], diagnostics), result.returncode


def _counts(diagnostics: list[Diagnostic]) -> Counter[str]:
    return Counter(_diagnostic_key(diagnostic) for diagnostic in diagnostics)


def _write_baseline(diagnostics: list[Diagnostic]) -> None:
    baseline = {
        "format": 1,
        "diagnostics": dict(sorted(_counts(diagnostics).items())),
    }
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    try:
        diagnostics, _ = _run_pyright()
        if "--update-baseline" in sys.argv[1:]:
            _write_baseline(diagnostics)
            print(f"Updated Pyright baseline with {len(diagnostics)} diagnostics.")
            return 0

        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if baseline.get("format") != 1 or not isinstance(baseline.get("diagnostics"), dict):
            raise RuntimeError(f"Unsupported or invalid baseline: {BASELINE_PATH}")

        current_counts = _counts(diagnostics)
        baseline_counts: dict[str, int] = baseline["diagnostics"]
        added = {
            key: count - baseline_counts.get(key, 0)
            for key, count in current_counts.items()
            if count > baseline_counts.get(key, 0)
        }
        if added:
            print("Pyright found diagnostics not covered by the approved baseline:")
            for key, count in sorted(added.items()):
                file_path, severity, rule, message = json.loads(key)
                print(f"{file_path}: {severity} ({rule or 'unclassified'}): {message} (+{count})")
            return 1

        print(
            f"Pyright baseline check passed: {len(diagnostics)} existing diagnostics; "
            "no new diagnostics."
        )
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Pyright baseline check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
