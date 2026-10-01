"""Offline commands for the contract suites. None of them sends a request to PipesHub.

    python -m helper.contract plan   <suite.yaml>   # list the generated test cases
    python -m helper.contract report <suite.yaml>   # write the report of the last run again
    python -m helper.contract accept <suite.yaml>   # make the baseline say what the last run found
    python -m helper.contract index                 # one page for the last run of every suite
    python -m helper.contract suites                # which suite has which operations
    python -m helper.contract overview              # one page: limited operations and fixtures

The run itself is a pytest test: `pytest -m contract`.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from helper.contract.baseline import BaselineError, write_baseline
from helper.contract.report import render_index, render_overview
from helper.contract.runner import (
    BASELINE_NAME,
    INDEX_PATH,
    INTEGRATION_TESTS_DIR,
    REPORTS_DIR,
    RunnerError,
    judge,
    plan,
    suite_paths,
)
from helper.contract.sources import fixture_rows, load_value_sources
from helper.contract.spec import all_operations, load_spec
from helper.contract.suite import PROFILE_SKIP, SuiteError, load_suite

_FOR_ONE_SUITE = ("plan", "report", "accept")
_FOR_ALL_SUITES = ("index", "suites", "overview")
OVERVIEW_NAME = "overview.md"


def baseline_path(suite_path: Path) -> Path:
    return suite_path.with_name(BASELINE_NAME)


def _index() -> Path:
    runs = []
    for suite_path in suite_paths():
        try:
            run = judge(load_suite(suite_path), baseline_path(suite_path))
        except RunnerError:
            continue
        runs.append((run.meta, list(run.results), run.files.report))
    if not runs:
        raise RunnerError(f"No run of any suite found in {REPORTS_DIR}.")
    INDEX_PATH.write_text(render_index(runs), encoding="utf-8")
    return INDEX_PATH


def _overview() -> Path:
    entries = []
    for suite_path in suite_paths():
        suite = load_suite(suite_path)
        rows = fixture_rows(suite, load_value_sources(suite_path))
        entries.append((suite, rows, str(suite_path.parent.relative_to(INTEGRATION_TESTS_DIR))))
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    overview = REPORTS_DIR / OVERVIEW_NAME
    overview.write_text(render_overview(entries), encoding="utf-8")
    return overview


def _suites() -> None:
    """Print every suite with its operations by profile, and what no suite has."""
    covered: Counter[str] = Counter()
    for suite_path in suite_paths():
        suite = load_suite(suite_path)
        profiles = Counter(planned.profile for planned in suite.operations)
        covered.update(planned.operation.label for planned in suite.operations)
        counts = ", ".join(f"{count} {profile}" for profile, count in sorted(profiles.items()))
        skipped = profiles[PROFILE_SKIP]
        print(
            f"{suite.name}: {len(suite.operations)} operations ({counts}); "
            f"{len(suite.operations) - skipped} sent  [{suite_path.relative_to(INTEGRATION_TESTS_DIR)}]"
        )
    operations = [operation.label for operation in all_operations(load_spec())]
    missing = [label for label in operations if label not in covered]
    print(
        f"\n{len(operations)} operations in the spec, {len(operations) - len(missing)} in a suite."
    )
    for label in missing:
        print(f"  in no suite: {label}")
    for label, count in covered.items():
        if count > 1:
            print(f"  in {count} suites: {label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m helper.contract",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=(*_FOR_ONE_SUITE, *_FOR_ALL_SUITES))
    parser.add_argument("suite", type=Path, nargs="?", help="Path to the suite file")
    args = parser.parse_args(argv)
    if (args.suite is None) == (args.command in _FOR_ONE_SUITE):
        parser.error(
            f"`{args.command}` takes "
            + ("a suite file" if args.command in _FOR_ONE_SUITE else "no suite file")
        )

    try:
        if args.command == "index":
            print(f"Index: {_index()}")
            return 0
        if args.command == "suites":
            _suites()
            return 0
        if args.command == "overview":
            print(f"Overview: {_overview()}")
            return 0
        suite = load_suite(args.suite)
        if args.command == "plan":
            fixtures = fixture_rows(suite, load_value_sources(args.suite))
            print(f"Plan: {plan(suite, fixtures=fixtures)}")
            return 0
        run = judge(suite, baseline_path(args.suite))
        if args.command == "accept":
            added, removed = write_baseline(baseline_path(args.suite), list(run.results))
            print(f"Baseline {baseline_path(args.suite)}: {added} added, {removed} removed")
            run = judge(suite, baseline_path(args.suite))
        print(f"Report: {run.files.report}")
        return 0
    except (SuiteError, RunnerError, BaselineError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
