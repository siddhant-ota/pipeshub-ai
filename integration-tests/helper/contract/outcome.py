"""Turn the verdict on an operation into the outcome of its pytest test."""

from __future__ import annotations

from pathlib import Path

import pytest

from helper.contract.results import (
    VERDICT_INCOMPLETE,
    VERDICT_KNOWN_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NOT_RUN,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED,
    VERDICT_STALE_BASELINE,
    VERDICT_STALE_SUITE,
    VERDICT_UNVERIFIED,
    Finding,
    OperationResult,
)

_MAX_LISTED = 15
# Verdicts for which `OperationResult.gap` says everything there is to say.
_FAILS_WITH_ITS_GAP = (
    VERDICT_UNVERIFIED,
    VERDICT_NOT_RUN,
    VERDICT_INCOMPLETE,
    VERDICT_STALE_SUITE,
)


def _listed(findings: list[Finding]) -> str:
    lines = [
        f"  - {finding.summary} [{finding.key.check}, {finding.count} case(s)]\n"
        f"      e.g. {finding.example_request[:200]}"
        for finding in sorted(findings, key=lambda f: str(f.key))[:_MAX_LISTED]
    ]
    if len(findings) > _MAX_LISTED:
        lines.append(f"  ... and {len(findings) - _MAX_LISTED} more")
    return "\n".join(lines)


def assert_spec_matches_api(result: OperationResult, report: Path) -> None:
    """Pass, skip, xfail or fail the calling test according to `result.verdict`.

    Fails for anything that leaves the contract of the operation unproven: a
    difference the baseline does not list, a baseline entry that no longer
    occurs, responses that do not show the operation was exercised, a run that
    did not complete, or no request sent at all.
    """
    verdict = result.verdict
    label = result.run.label
    if verdict in (VERDICT_MATCH, VERDICT_PARTIAL):
        return
    if verdict == VERDICT_SKIPPED:
        pytest.skip(result.run.reason)
    if verdict == VERDICT_KNOWN_MISMATCH:
        pytest.xfail(
            f"{len(result.findings)} known difference(s) between the spec and the API, "
            f"all listed in the baseline. Report: {report}"
        )
    if verdict == VERDICT_MISMATCH:
        new = result.new_findings
        pytest.fail(
            f"{label}: the spec does not describe what the API does. "
            f"{len(new)} difference(s) are not in the baseline:\n{_listed(new)}\n"
            f"Full report: {report}\n"
            "Fix the spec, or accept the differences with "
            "`python -m helper.contract accept <suite.yaml>`.",
            pytrace=False,
        )
    if verdict == VERDICT_STALE_BASELINE:
        stale = "\n".join(f"  - {key}" for key in result.stale)
        pytest.fail(
            f"{label}: the baseline lists {len(result.stale)} difference(s) that no longer occur:\n"
            f"{stale}\nRemove them with `python -m helper.contract accept <suite.yaml>`.",
            pytrace=False,
        )
    if verdict in _FAILS_WITH_ITS_GAP:
        pytest.fail(f"{label}: {result.gap}", pytrace=False)
    raise AssertionError(f"{label}: no pytest outcome for verdict {verdict!r}")
