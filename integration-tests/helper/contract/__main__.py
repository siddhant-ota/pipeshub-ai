"""Offline commands for a contract suite. None of them sends a request to PipesHub.

    python -m helper.contract plan   <suite.yaml>   # list the generated test cases
    python -m helper.contract report <suite.yaml>   # write the report of the last run again
    python -m helper.contract accept <suite.yaml>   # make the baseline say what the last run found

The run itself is a pytest test: `pytest -m contract`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from helper.contract.baseline import write_baseline
from helper.contract.runner import RunnerError, judge, plan
from helper.contract.suite import SuiteError, load_suite

BASELINE_NAME = "baseline.json"


def baseline_path(suite_path: Path) -> Path:
    return suite_path.parent / BASELINE_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m helper.contract",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("plan", "report", "accept"))
    parser.add_argument("suite", type=Path, help="Path to the suite file")
    args = parser.parse_args(argv)

    try:
        suite = load_suite(args.suite)
        if args.command == "plan":
            print(f"Plan: {plan(suite)}")
            return 0
        run = judge(suite, baseline_path(args.suite))
        if args.command == "accept":
            added, removed = write_baseline(baseline_path(args.suite), list(run.results))
            print(f"Baseline {baseline_path(args.suite)}: {added} added, {removed} removed")
            run = judge(suite, baseline_path(args.suite))
        print(f"Report: {run.files.report}")
        return 0
    except (SuiteError, RunnerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
