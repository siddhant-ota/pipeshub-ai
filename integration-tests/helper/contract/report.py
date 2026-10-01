"""Write the report of a run and the plan of a suite as Markdown and JSON."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from helper.contract.config import (
    STATE_EXAMPLES_ONLY,
    STATE_FULL,
    STATE_NEGATIVE_ONLY,
    OperationRun,
)
from helper.contract.events import read_cases
from helper.contract.results import (
    RESPONSE_SCHEMA,
    VERDICT_DESELECTED,
    VERDICT_INCOMPLETE,
    VERDICT_KNOWN_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NOT_RUN,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED,
    VERDICT_STALE_BASELINE,
    VERDICT_STALE_SUITE,
    VERDICT_UNAVAILABLE,
    VERDICT_UNVERIFIED,
    Finding,
    OperationResult,
)
from helper.contract.sources import FixtureRow
from helper.contract.suite import (
    PROFILE_EXAMPLES_ONLY,
    PROFILE_FULL,
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    Suite,
)

_VERDICT_TEXT = {
    VERDICT_MISMATCH: (
        "Differs",
        "A difference between the spec and the API that the baseline does not list",
    ),
    VERDICT_STALE_BASELINE: (
        "Stale baseline",
        "The baseline lists a difference that no longer occurs",
    ),
    VERDICT_STALE_SUITE: (
        "Stale suite entry",
        "The suite file says no request can succeed, and one did",
    ),
    VERDICT_INCOMPLETE: (
        "Incomplete",
        "A request got no response, or Schemathesis could not finish the operation",
    ),
    VERDICT_NOT_RUN: ("Not run", "No request was sent: a value the operation needs is missing"),
    VERDICT_UNVERIFIED: (
        "Unverified",
        "No difference, but the responses that would show one never came",
    ),
    VERDICT_KNOWN_MISMATCH: (
        "Known difference",
        "Differences found; the baseline lists all of them",
    ),
    VERDICT_PARTIAL: (
        "Partly checked",
        "No difference; by design the success response is not checked",
    ),
    VERDICT_UNAVAILABLE: (
        "Not possible here",
        "A fixture skipped: this deployment cannot give the operation what it needs",
    ),
    VERDICT_SKIPPED: ("Skipped", "The suite file skips the operation"),
    VERDICT_MATCH: ("Matches", "The spec and the API agree, and a success response was checked"),
}
_VERDICT_ORDER = tuple(_VERDICT_TEXT)
_STATE_TEXT = {
    STATE_FULL: "full",
    STATE_EXAMPLES_ONLY: "invalid requests, and the valid examples of the spec",
    STATE_NEGATIVE_ONLY: "invalid requests only",
}
_MESSAGE_LINES = 12
_EXAMPLE_CHARS = 300


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    if not rows:
        return ["None.", ""]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return [*lines, ""]


def _name(run: OperationRun) -> str:
    return f"`{run.label}`"


def _statuses(result: OperationResult) -> str:
    return ", ".join(f"{status}×{count}" for status, count in sorted(result.statuses.items()))


def _bold_label(sentence: str) -> str:
    """`Contract: DIFFERS — ...` -> `**Contract: DIFFERS** — ...`."""
    label, separator, rest = sentence.partition(" — ")
    return f"**{label}**{separator}{rest}"


def _finding_lines(finding: Finding) -> list[str]:
    marker = "known" if finding.known else "new"
    example = finding.example_request.replace("`", "'")
    if len(example) > _EXAMPLE_CHARS:
        example = example[:_EXAMPLE_CHARS] + "…"
    lines = [
        f"- **{marker}** — {finding.summary} (`{finding.key.check}`, {finding.count} case(s))",
        f"  - Example: `{example}`",
    ]
    if finding.key.check == RESPONSE_SCHEMA and finding.example_message:
        message = finding.example_message.splitlines()[:_MESSAGE_LINES]
        lines += ["", "    ```", *(f"    {line}" for line in message), "    ```", ""]
    return lines


def _fixture_table(
    fixtures: list[FixtureRow], *, with_values: bool, name_operations: bool = False
) -> list[str]:
    def _values(row: FixtureRow) -> str:
        if not with_values:
            return ", ".join(f"`{key}`" for key in row.keys)
        if row.problem:
            return f"**no value** — {row.problem}"
        return ", ".join(f"`{key}` = `{row.values.get(key, '')}`" for key in row.keys)

    return _table(
        ["Fixture", "Origin", "What it is", "Values", "Operations that use it"],
        [
            [
                f"`{row.fixture}`",
                "added" if row.added else "existing",
                row.what,
                _values(row),
                ", ".join(row.operations) if name_operations else len(row.operations),
            ]
            for row in sorted(fixtures, key=lambda row: (not row.added, row.fixture))
        ],
    )


def _fixture_section(fixtures: list[FixtureRow], *, with_values: bool) -> list[str]:
    """The fixtures of the suite: what each one is, and the values it gave."""
    if not fixtures:
        return []
    return [
        "## Fixtures",
        "",
        "Where the real values in the requests come from. `added` is a fixture that was written",
        "for the contract tests; `existing` is one that the integration tests already had.",
        "",
        *_fixture_table(fixtures, with_values=with_values),
    ]


def _headline(results: list[OperationResult]) -> tuple[str, str]:
    """The two sentences that say how the run went: the contract, and how much of it was checked."""
    new = sum(len(result.new_findings) for result in results)
    known = sum(len(result.known_findings) for result in results)
    stale = sum(len(result.stale) for result in results)
    gaps = sum(bool(result.gap) for result in results)
    contract = (
        "DIFFERS" if new or stale else "MATCHES (with known differences)" if known else "MATCHES"
    )
    coverage = "INCOMPLETE" if gaps else "COMPLETE"
    return (
        f"Contract: {contract} — {new} new difference(s), {known} known, {stale} stale in the baseline.",
        f"Coverage: {coverage} — {gaps} of {len(results)} operations have a coverage gap.",
    )


def summary_lines(results: list[OperationResult]) -> list[str]:
    """A short text summary of a run, with one line for each coverage gap."""
    results = [result for result in results if result.verdict != VERDICT_DESELECTED]
    gaps = [result for result in results if result.gap]
    return [
        *_headline(results),
        *(
            f"  {_VERDICT_TEXT[result.verdict][0]}: {result.run.label} — {result.gap}"
            for result in sorted(gaps, key=lambda r: (_VERDICT_ORDER.index(r.verdict), r.run.path))
        ),
    ]


def render_report(
    results: list[OperationResult], meta: dict[str, Any], fixtures: list[FixtureRow] | None = None
) -> str:
    results = [result for result in results if result.verdict != VERDICT_DESELECTED]
    verdicts = Counter(result.verdict for result in results)
    gaps = [result for result in results if result.gap]
    contract, coverage = _headline(results)
    server_errors = [
        result for result in results if any(status.startswith("5") for status in result.statuses)
    ]

    lines = [
        f"# API contract report: {meta['suite']}",
        "",
        f"- Target: `{meta['target']}`",
        f"- Run: {meta['time']}, Schemathesis {meta['schemathesis']}, seed {meta['seed']}",
        f"- Spec commit: `{meta['spec_commit']}`",
        f"- Test cases sent: {sum(result.cases for result in results)}",
        "",
        _bold_label(contract),
        "",
        _bold_label(coverage),
        "",
        "The API is the reference: a difference means the spec does not describe what the API does.",
        "",
        "## Verdicts",
        "",
        *_table(
            ["Verdict", "Operations", "Meaning"],
            [
                [_VERDICT_TEXT[verdict][0], verdicts[verdict], _VERDICT_TEXT[verdict][1]]
                for verdict in _VERDICT_ORDER
            ],
        ),
        "## Coverage gaps",
        "",
        "Operations whose contract was not fully checked, and why.",
        "",
        *_table(
            ["Operation", "operationId", "Verdict", "Why"],
            [
                [
                    _name(result.run),
                    result.run.operation_id,
                    _VERDICT_TEXT[result.verdict][0],
                    result.gap,
                ]
                for result in sorted(
                    gaps, key=lambda r: (_VERDICT_ORDER.index(r.verdict), r.run.path)
                )
            ],
        ),
        "## Differences between the spec and the API",
        "",
    ]

    with_findings = [result for result in results if result.findings or result.stale]
    if not with_findings:
        lines += ["None.", ""]
    for result in sorted(
        with_findings, key=lambda r: (not r.new_findings, not r.run.sdk, r.run.path)
    ):
        lines += [f"### {_name(result.run)} — {result.run.operation_id}", ""]
        for finding in sorted(result.findings.values(), key=lambda f: (f.known, str(f.key))):
            lines += _finding_lines(finding)
        for key in result.stale:
            lines.append(
                f"- **stale baseline entry** — {key}: the run did not reproduce it; remove it from the baseline"
            )
        lines.append("")

    lines += [
        "## 5xx responses",
        "",
        "For information. A 5xx is a difference only when the spec does not document it; those are listed above.",
        "",
        *_table(
            ["Operation", "operationId", "Responses"],
            [
                [_name(result.run), result.run.operation_id, _statuses(result)]
                for result in server_errors
            ],
        ),
        "## All operations",
        "",
        *_table(
            ["Operation", "operationId", "SDK", "Verdict", "Cases", "Responses", "New", "Known"],
            [
                [
                    _name(result.run),
                    result.run.operation_id,
                    "yes" if result.run.sdk else "",
                    _VERDICT_TEXT[result.verdict][0],
                    f"{result.cases} ({result.positive} valid, {result.negative} invalid)"
                    if result.cases
                    else "",
                    _statuses(result),
                    len(result.new_findings) or "",
                    len(result.known_findings) or "",
                ]
                for result in sorted(
                    results,
                    key=lambda r: (_VERDICT_ORDER.index(r.verdict), not r.run.sdk, r.run.path),
                )
            ],
        ),
    ]
    ignored = sum(result.ignored for result in results)
    unjudged = sum(result.unjudged for result in results)
    if ignored or unjudged:
        lines += ["## Failed checks that are not counted", ""]
    if ignored:
        lines += [
            f"- {ignored}: the API accepted an unknown query, header or cookie parameter. "
            + "OpenAPI cannot forbid one, so that is not a difference from the spec.",
        ]
    if unjudged:
        lines += [
            f"- {unjudged}: the API rejected a valid request that had a generated value in an ID field "
            + "that has no fixture (`ids_without_fixture`). The request named something that does not exist, "
            + "so the rejection says nothing about the spec. A fixture for that field would close the gap.",
        ]
    if ignored or unjudged:
        lines.append("")
    lines += _fixture_section(fixtures or [], with_values=True)
    return "\n".join(lines)


def report_json(
    results: list[OperationResult], meta: dict[str, Any], fixtures: list[FixtureRow] | None = None
) -> dict[str, Any]:
    return {
        "meta": meta,
        "fixtures": [asdict(row) for row in fixtures or []],
        "operations": [
            {
                **asdict(result.run),
                "verdict": result.verdict,
                "gap": result.gap,
                "cases": result.cases,
                "positive": result.positive,
                "negative": result.negative,
                "statuses": dict(result.statuses),
                "findings": [
                    {
                        "check": finding.key.check,
                        "subject": finding.key.subject,
                        "detail": finding.key.detail,
                        "known": finding.known,
                        "count": finding.count,
                        "summary": finding.summary,
                        "example_request": finding.example_request,
                        "example_status": finding.example_status,
                        "example_message": finding.example_message,
                    }
                    for finding in result.findings.values()
                ],
                "stale_baseline": [asdict(key) for key in result.stale],
            }
            for result in results
        ],
    }


def write_report(
    results: list[OperationResult],
    meta: dict[str, Any],
    fixtures: list[FixtureRow],
    directory: Path,
) -> None:
    (directory / "report.md").write_text(render_report(results, meta, fixtures), encoding="utf-8")
    (directory / "report.json").write_text(
        json.dumps(report_json(results, meta, fixtures), indent=2), encoding="utf-8"
    )


def render_index(runs: list[tuple[dict[str, Any], list[OperationResult], Path]]) -> str:
    """One page for several suites: (meta, results, report file) of the last run of each."""
    rows = []
    everything: list[OperationResult] = []
    for meta, results, report in sorted(runs, key=lambda run: run[0]["suite"]):
        results = [result for result in results if result.verdict != VERDICT_DESELECTED]
        everything += results
        verdicts = Counter(result.verdict for result in results)
        rows.append(
            [
                f"[{meta['suite']}]({report})",
                meta["time"],
                len(results),
                *(verdicts[verdict] or "" for verdict in _VERDICT_ORDER),
            ]
        )
    contract, coverage = _headline(everything)
    return "\n".join(
        [
            "# API contract report: all suites",
            "",
            "The last run of each suite. A suite has its own report with the details.",
            "",
            _bold_label(contract),
            "",
            _bold_label(coverage),
            "",
            *_table(
                ["Suite", "Run", "Operations", *(_VERDICT_TEXT[v][0] for v in _VERDICT_ORDER)],
                rows,
            ),
            "## Verdicts",
            "",
            *_table(
                ["Verdict", "Meaning"],
                [
                    [_VERDICT_TEXT[verdict][0], _VERDICT_TEXT[verdict][1]]
                    for verdict in _VERDICT_ORDER
                ],
            ),
        ]
    )


def render_plan(
    runs: list[OperationRun],
    ndjson_path: Path,
    meta: dict[str, Any],
    fixtures: list[FixtureRow] | None = None,
) -> str:
    by_label: dict[str, Counter[tuple[str, str]]] = {}
    for case in read_cases(ndjson_path):
        mode = "invalid" if case.is_negative else "valid"
        by_label.setdefault(case.label, Counter())[
            (mode, case.what or "Example from the spec")
        ] += 1

    def _count(run: OperationRun, mode: str | None = None) -> int:
        counter = by_label.get(run.label, Counter())
        return sum(n for (case_mode, _), n in counter.items() if mode in (None, case_mode))

    sent = [run for run in runs if run.is_sent]
    not_sent = [run for run in runs if not run.is_sent]
    total = sum(_count(run) for run in sent)
    valid = sum(_count(run, "valid") for run in sent)
    lines = [
        f"# Contract test plan: {meta['suite']}",
        "",
        "The test cases that Schemathesis generates from the spec for this suite. They come",
        "from its `examples` and `coverage` phases, which are deterministic, so a run against",
        "PipesHub sends the same cases. The plan uses placeholder IDs; a run uses real ones.",
        "",
        f"- Generated: {meta['time']}, Schemathesis {meta['schemathesis']}, seed {meta['seed']}",
        f"- Spec commit: `{meta['spec_commit']}`",
        f"- Operations in scope: {len(runs)} ({len(sent)} sent, {len(not_sent)} not sent)",
        f"- Test cases: {total} ({valid} valid, {total - valid} invalid)",
        "",
        "A valid case is a request the spec allows; the API is expected to accept it.",
        "An invalid case is a request the spec forbids; the API is expected to reject it.",
        "",
        "## Operations",
        "",
        *_table(
            ["Operation", "operationId", "SDK", "Login", "Sent", "Cases", "Valid", "Invalid"],
            [
                [
                    _name(run),
                    run.operation_id,
                    "yes" if run.sdk else "",
                    run.auth,
                    _STATE_TEXT[run.state],
                    _count(run),
                    _count(run, "valid"),
                    _count(run, "invalid"),
                ]
                for run in sent
            ],
        ),
        "## Not sent",
        "",
        *_table(
            ["Operation", "operationId", "Reason"],
            [[_name(run), run.operation_id, run.reason] for run in not_sent],
        ),
        *_fixture_section(fixtures or [], with_values=False),
        "## Test cases for each operation",
        "",
    ]
    for run in sent:
        counter = by_label.get(run.label, Counter())
        lines += [
            f"### {_name(run)} — {run.operation_id}",
            "",
            *_table(
                ["Case", "What it sends", "Cases"],
                [[mode, what, count] for (mode, what), count in sorted(counter.items())],
            ),
        ]
    return "\n".join(lines)


def write_plan(
    runs: list[OperationRun],
    ndjson_path: Path,
    meta: dict[str, Any],
    fixtures: list[FixtureRow],
    directory: Path,
) -> Path:
    path = directory / "plan.md"
    path.write_text(render_plan(runs, ndjson_path, meta, fixtures), encoding="utf-8")
    return path


_PROFILE_TEXT = {
    PROFILE_FULL: "in full",
    PROFILE_EXAMPLES_ONLY: "invalid requests and spec examples",
    PROFILE_NEGATIVE_ONLY: "invalid requests only",
    PROFILE_SKIP: "not sent",
}


def render_overview(suites: list[tuple[Suite, list[FixtureRow], str]]) -> str:
    """One page that says, without a run, what every suite does: (suite, fixtures, folder).

    It is for a person who checks the suites: which operations are limited and
    why, and which fixtures exist and what they create.
    """
    planned = [operation for suite, _, _ in suites for operation in suite.operations]
    profiles = Counter(operation.profile for operation in planned)
    added = sum(row.added for _, rows, _ in suites for row in rows)
    lines = [
        "# API contract suites: what they do",
        "",
        "Made from the suite files and their fixtures; no run is needed for it.",
        "",
        f"- Suites: {len(suites)}",
        f"- Operations: {len(planned)} — "
        + ", ".join(f"{profiles[profile]} {text}" for profile, text in _PROFILE_TEXT.items()),
        f"- Operations that are sent and declare that no request can succeed: "
        f"{sum(bool(operation.no_success_reason) for operation in planned)}",
        f"- Fixtures: {sum(len(rows) for _, rows, _ in suites)} ({added} added for the contract tests)",
        "",
        *_table(
            ["Suite", "Folder", "Operations", *_PROFILE_TEXT.values(), "Fixtures"],
            [
                [
                    suite.name,
                    f"`{folder}`",
                    len(suite.operations),
                    *(
                        sum(operation.profile == profile for operation in suite.operations) or ""
                        for profile in _PROFILE_TEXT
                    ),
                    len(rows),
                ]
                for suite, rows, folder in suites
            ],
        ),
    ]
    for suite, rows, folder in suites:
        limited = [op for op in suite.operations if op.profile != PROFILE_FULL]
        no_success = [op for op in suite.operations if op.no_success_reason]
        logins = Counter(op.auth for op in suite.operations if op.profile != PROFILE_SKIP)
        lines += [
            f"## {suite.name}",
            "",
            f"`{folder}` — {len(suite.operations)} operations. Logins: "
            + ", ".join(f"{count} {login}" for login, count in sorted(logins.items()))
            + ".",
            "",
            "### Operations that are not run in full",
            "",
            *_table(
                ["Operation", "operationId", "How it is run", "Why"],
                [
                    [
                        f"`{op.operation.label}`",
                        op.operation.operation_id,
                        _PROFILE_TEXT[op.profile],
                        op.reason,
                    ]
                    for op in limited
                ],
            ),
            "### Operations that are sent, and for which no request can succeed",
            "",
            *_table(
                ["Operation", "operationId", "Why"],
                [
                    [f"`{op.operation.label}`", op.operation.operation_id, op.no_success_reason]
                    for op in no_success
                ],
            ),
            "### Fixtures",
            "",
            *_fixture_table(rows, with_values=False, name_operations=True),
        ]
    return "\n".join(lines)
