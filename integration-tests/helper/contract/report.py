"""Write the report of a run and the plan of a suite as Markdown and JSON."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from helper.contract.config import STATE_FULL, STATE_NEGATIVE_ONLY, OperationRun
from helper.contract.events import read_cases
from helper.contract.results import (
    RESPONSE_SCHEMA,
    VERDICT_DESELECTED,
    VERDICT_KNOWN_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NOT_RUN,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED,
    VERDICT_STALE_BASELINE,
    VERDICT_UNVERIFIED,
    Finding,
    OperationResult,
)

_VERDICT_TEXT = {
    VERDICT_MISMATCH: ("Differs", "A difference between the spec and the API that the baseline does not list"),
    VERDICT_STALE_BASELINE: ("Stale baseline", "The baseline lists a difference that no longer occurs"),
    VERDICT_NOT_RUN: ("Not run", "No request was sent: a value the operation needs is missing"),
    VERDICT_UNVERIFIED: ("Unverified", "No difference, but no 2xx response was checked"),
    VERDICT_KNOWN_MISMATCH: ("Known difference", "Differences found; the baseline lists all of them"),
    VERDICT_PARTIAL: ("Partly checked", "No difference; by design the success response is not checked"),
    VERDICT_SKIPPED: ("Skipped", "The suite file skips the operation"),
    VERDICT_MATCH: ("Matches", "The spec and the API agree, and a success response was checked"),
}
_VERDICT_ORDER = tuple(_VERDICT_TEXT)
# Verdicts that leave part of the operation's contract unchecked.
_GAP_VERDICTS = (VERDICT_NOT_RUN, VERDICT_UNVERIFIED, VERDICT_PARTIAL, VERDICT_SKIPPED)
_STATE_TEXT = {STATE_FULL: "full", STATE_NEGATIVE_ONLY: "invalid requests only"}
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


def render_report(results: list[OperationResult], meta: dict[str, Any]) -> str:
    results = [result for result in results if result.verdict != VERDICT_DESELECTED]
    verdicts = Counter(result.verdict for result in results)
    new = sum(len(result.new_findings) for result in results)
    known = sum(len(result.known_findings) for result in results)
    stale = sum(len(result.stale) for result in results)
    gaps = [result for result in results if result.verdict in _GAP_VERDICTS]
    server_errors = [
        result for result in results if any(status.startswith("5") for status in result.statuses)
    ]

    contract = "DIFFERS" if new or stale else "MATCHES (with known differences)" if known else "MATCHES"
    coverage = "INCOMPLETE" if gaps else "COMPLETE"
    lines = [
        f"# API contract report: {meta['suite']}",
        "",
        f"- Target: `{meta['target']}`",
        f"- Run: {meta['time']}, Schemathesis {meta['schemathesis']}, seed {meta['seed']}",
        f"- Spec commit: `{meta['spec_commit']}`",
        f"- Test cases sent: {sum(result.cases for result in results)}",
        "",
        f"**Contract: {contract}** — {new} new difference(s), {known} known, {stale} stale in the baseline.",
        "",
        f"**Coverage: {coverage}** — {len(gaps)} of {len(results)} operations have a coverage gap.",
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
                [_name(result.run), result.run.operation_id, _VERDICT_TEXT[result.verdict][0], result.gap]
                for result in sorted(gaps, key=lambda r: (_VERDICT_ORDER.index(r.verdict), r.run.path))
            ],
        ),
        "## Differences between the spec and the API",
        "",
    ]

    with_findings = [result for result in results if result.findings or result.stale]
    if not with_findings:
        lines += ["None.", ""]
    for result in sorted(with_findings, key=lambda r: (not r.new_findings, not r.run.sdk, r.run.path)):
        lines += [f"### {_name(result.run)} — {result.run.operation_id}", ""]
        for finding in sorted(result.findings.values(), key=lambda f: (f.known, str(f.key))):
            lines += _finding_lines(finding)
        for key in result.stale:
            lines.append(f"- **stale baseline entry** — {key}: the run did not reproduce it; remove it from the baseline")
        lines.append("")

    lines += [
        "## 5xx responses",
        "",
        "For information. A 5xx is a difference only when the spec does not document it; those are listed above.",
        "",
        *_table(
            ["Operation", "operationId", "Responses"],
            [[_name(result.run), result.run.operation_id, _statuses(result)] for result in server_errors],
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
                    f"{result.cases} ({result.positive} valid, {result.negative} invalid)" if result.cases else "",
                    _statuses(result),
                    len(result.new_findings) or "",
                    len(result.known_findings) or "",
                ]
                for result in sorted(
                    results, key=lambda r: (_VERDICT_ORDER.index(r.verdict), not r.run.sdk, r.run.path)
                )
            ],
        ),
    ]
    ignored = sum(result.ignored for result in results)
    if ignored:
        note = (
            f"{ignored} failed check(s) were left out: the API accepted an unknown query, header or "
            "cookie parameter. OpenAPI cannot forbid one, so that is not a difference from the spec."
        )
        lines += [note, ""]
    return "\n".join(lines)


def report_json(results: list[OperationResult], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": meta,
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


def write_report(results: list[OperationResult], meta: dict[str, Any], directory: Path) -> None:
    (directory / "report.md").write_text(render_report(results, meta), encoding="utf-8")
    (directory / "report.json").write_text(
        json.dumps(report_json(results, meta), indent=2), encoding="utf-8"
    )


def render_plan(runs: list[OperationRun], ndjson_path: Path, meta: dict[str, Any]) -> str:
    by_label: dict[str, Counter[tuple[str, str]]] = {}
    for case in read_cases(ndjson_path):
        mode = "invalid" if case.is_negative else "valid"
        by_label.setdefault(case.label, Counter())[(mode, case.what or "Example from the spec")] += 1

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
            ["Operation", "operationId", "SDK", "Sent", "Cases", "Valid", "Invalid"],
            [
                [
                    _name(run),
                    run.operation_id,
                    "yes" if run.sdk else "",
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
    runs: list[OperationRun], ndjson_path: Path, meta: dict[str, Any], directory: Path
) -> Path:
    path = directory / "plan.md"
    path.write_text(render_plan(runs, ndjson_path, meta), encoding="utf-8")
    return path
