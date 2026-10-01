"""Turn Schemathesis results into the test plan and the report card."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config_builder import (
    STATE_NEGATIVE_ONLY,
    STATE_TESTED,
    OperationRun,
)
from events import Case, read_cases

RESPONSE_CHECKS = frozenset(
    {"status_code_conformance", "content_type_conformance", "response_schema_conformance"}
)
CONSTRAINT_CHECKS = frozenset({"positive_data_acceptance", "negative_data_rejection"})
SERVER_CHECK = "not_a_server_error"

GRADE_VERIFIED = "Verified"
GRADE_MATCHES = "Matches"
GRADE_DRIFT = "Drift"
GRADE_SERVER_ERROR = "Server error"
GRADE_PARTLY_TESTED = "Partly tested"
GRADE_NOT_TESTED = "Not tested"
GRADE_ORDER = (
    GRADE_DRIFT,
    GRADE_SERVER_ERROR,
    GRADE_PARTLY_TESTED,
    GRADE_NOT_TESTED,
    GRADE_MATCHES,
    GRADE_VERIFIED,
)

_STATE_TEXT = {STATE_TESTED: "full", STATE_NEGATIVE_ONLY: "negative only"}
_MESSAGE_LINES = 12
_BODY_CHARS = 240


@dataclass
class Finding:
    """One distinct failure of one check on one operation."""

    check: str
    title: str
    detail: str
    count: int = 0
    example_request: str = ""
    example_status: int | None = None
    example_message: str = ""


@dataclass
class OperationResult:
    run: OperationRun
    cases: int = 0
    positive: int = 0
    negative: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    findings: dict[tuple[str, str, str], Finding] = field(default_factory=dict)

    @property
    def success_responses(self) -> int:
        return sum(count for status, count in self.statuses.items() if status.startswith("2"))

    def findings_for(self, checks: frozenset[str]) -> list[Finding]:
        return [finding for finding in self.findings.values() if finding.check in checks]

    @property
    def grade(self) -> str:
        if not self.run.is_sent or not self.cases:
            return GRADE_NOT_TESTED
        if self.findings_for(RESPONSE_CHECKS | CONSTRAINT_CHECKS):
            return GRADE_DRIFT
        if self.findings_for(frozenset({SERVER_CHECK})):
            return GRADE_SERVER_ERROR
        # Without a 2xx answer, nothing checked the success response against the spec.
        if self.run.state == STATE_NEGATIVE_ONLY or not self.success_responses:
            return GRADE_PARTLY_TESTED
        return GRADE_VERIFIED if self.run.sdk else GRADE_MATCHES

    @property
    def note(self) -> str:
        if not self.run.is_sent:
            return self.run.reason
        if not self.cases:
            return "Schemathesis ran no test case for this operation."
        if self.run.state == STATE_NEGATIVE_ONLY:
            return f"Negative cases only. {self.run.reason}"
        if not self.success_responses:
            return "No 2xx response, so the success response was not checked."
        return ""


def _finding_detail(case: Case, check_name: str, message: str) -> str:
    if check_name in CONSTRAINT_CHECKS or check_name == SERVER_CHECK:
        return f"{case.what} → HTTP {case.status}"
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    return f"HTTP {case.status}: {first_line}" if first_line else f"HTTP {case.status}"


def _example_request(case: Case) -> str:
    request = f"{case.method} {case.target}"
    body = case.request_body
    if not body:
        return request
    if len(body) > _BODY_CHARS:
        body = body[:_BODY_CHARS] + "…"
    return f"{request} {body}"


def collect(runs: list[OperationRun], ndjson_path: Path | None) -> list[OperationResult]:
    results = {f"{run.method} {run.path}": OperationResult(run) for run in runs}
    if ndjson_path is None or not ndjson_path.exists():
        return list(results.values())
    for case in read_cases(ndjson_path):
        result = results.get(case.label)
        if result is None:
            continue
        result.cases += 1
        if case.mode == "negative":
            result.negative += 1
        else:
            result.positive += 1
        result.statuses[str(case.status) if case.status is not None else "no response"] += 1
        for check in case.checks:
            if check.passed:
                continue
            detail = _finding_detail(case, check.name, check.message)
            finding = result.findings.setdefault(
                (check.name, check.title, detail),
                Finding(
                    check=check.name,
                    title=check.title,
                    detail=detail,
                    example_request=_example_request(case),
                    example_status=case.status,
                    example_message=check.message,
                ),
            )
            finding.count += 1
    return list(results.values())


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _operation_name(run: OperationRun) -> str:
    return f"`{run.method} {run.path}`"


def _statuses(result: OperationResult) -> str:
    return ", ".join(f"{status}×{count}" for status, count in sorted(result.statuses.items()))


def render_report(results: list[OperationResult], meta: dict[str, Any]) -> str:
    grades = Counter(result.grade for result in results)
    lines = [
        f"# API contract report: {meta['suite']}",
        "",
        f"- Target: `{meta['target']}`",
        f"- Run: {meta['time']}, Schemathesis {meta['schemathesis']}, seed {meta['seed']}",
        f"- Spec commit: `{meta['commit']}`",
        f"- Test cases sent: {sum(result.cases for result in results)}",
        "",
        "## Grades",
        "",
        *_table(
            ["Grade", "Operations", "Meaning"],
            [
                [GRADE_VERIFIED, grades[GRADE_VERIFIED], "In the SDK, tested, no failure"],
                [GRADE_MATCHES, grades[GRADE_MATCHES], "Tested, no failure"],
                [GRADE_DRIFT, grades[GRADE_DRIFT], "The API and the spec do not agree"],
                [GRADE_SERVER_ERROR, grades[GRADE_SERVER_ERROR], "The API returned 5xx"],
                [
                    GRADE_PARTLY_TESTED,
                    grades[GRADE_PARTLY_TESTED],
                    "No failure, but the success response was not checked",
                ],
                [GRADE_NOT_TESTED, grades[GRADE_NOT_TESTED], "No request was sent"],
            ],
        ),
        "",
        "## Operations",
        "",
        *_table(
            ["Operation", "operationId", "SDK", "Grade", "Cases", "Responses", "Response", "Constraints", "5xx", "Note"],
            [
                [
                    _operation_name(result.run),
                    result.run.operation_id,
                    "yes" if result.run.sdk else "",
                    result.grade,
                    f"{result.cases} ({result.positive}+ / {result.negative}−)" if result.cases else "",
                    _statuses(result),
                    len(result.findings_for(RESPONSE_CHECKS)) or "",
                    len(result.findings_for(CONSTRAINT_CHECKS)) or "",
                    len(result.findings_for(frozenset({SERVER_CHECK}))) or "",
                    result.note,
                ]
                for result in sorted(
                    results, key=lambda r: (GRADE_ORDER.index(r.grade), not r.run.sdk, r.run.path)
                )
            ],
        ),
        "",
        "The columns Response, Constraints and 5xx count distinct findings, not test cases.",
        "",
        "## Findings",
        "",
    ]
    with_findings = [result for result in results if result.findings]
    if not with_findings:
        lines.append("No findings.")
    for result in sorted(with_findings, key=lambda r: (not r.run.sdk, r.run.path)):
        lines += [f"### {_operation_name(result.run)} — {result.run.operation_id}", ""]
        for finding in sorted(result.findings.values(), key=lambda f: (f.check, -f.count)):
            example = finding.example_request.replace("`", "'")
            lines += [
                f"- **{finding.title or finding.check}** (`{finding.check}`, {finding.count} case(s)): {finding.detail}",
                f"  - Example: `{example}`",
            ]
            if finding.check in RESPONSE_CHECKS and finding.example_message:
                message = finding.example_message.splitlines()[:_MESSAGE_LINES]
                lines += ["", "    ```", *(f"    {line}" for line in message), "    ```", ""]
        lines.append("")
    return "\n".join(lines) + "\n"


def report_json(results: list[OperationResult], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": meta,
        "operations": [
            {
                **asdict(result.run),
                "grade": result.grade,
                "note": result.note,
                "cases": result.cases,
                "positive": result.positive,
                "negative": result.negative,
                "statuses": dict(result.statuses),
                "findings": [asdict(finding) for finding in result.findings.values()],
            }
            for result in results
        ],
    }


def render_plan(runs: list[OperationRun], ndjson_path: Path, meta: dict[str, Any]) -> str:
    """List the generated test cases. Built from a run against the stub server."""
    by_label: dict[str, Counter[tuple[str, str]]] = {}
    for case in read_cases(ndjson_path):
        by_label.setdefault(case.label, Counter())[(case.mode or "positive", case.what)] += 1

    def _count(run: OperationRun, mode: str | None = None) -> int:
        counter = by_label.get(f"{run.method} {run.path}", Counter())
        return sum(n for (case_mode, _), n in counter.items() if mode in (None, case_mode))

    sent = [run for run in runs if run.is_sent]
    not_sent = [run for run in runs if not run.is_sent]
    total = sum(_count(run) for run in sent)
    positive = sum(_count(run, "positive") for run in sent)
    lines = [
        f"# Contract test plan: {meta['suite']}",
        "",
        "These are the test cases that Schemathesis generates from the spec for this suite.",
        "They come from the `examples` and `coverage` phases, which are deterministic,",
        "so a run against PipesHub sends the same cases. The plan uses placeholder IDs",
        "for path parameters; a real run uses the fixture IDs.",
        "",
        f"- Generated: {meta['time']}, Schemathesis {meta['schemathesis']}, seed {meta['seed']}",
        f"- Spec commit: `{meta['commit']}`",
        f"- Operations in scope: {len(runs)} ({len(sent)} sent, {len(not_sent)} not sent)",
        f"- Test cases: {total} ({positive} positive, {total - positive} negative)",
        "",
        "A positive case sends a request that the spec allows, and expects the API to accept it.",
        "A negative case sends a request that the spec forbids, and expects the API to reject it.",
        "",
        "## Operations",
        "",
        *_table(
            ["Operation", "operationId", "SDK", "Run", "Cases", "Positive", "Negative"],
            [
                [
                    _operation_name(run),
                    run.operation_id,
                    "yes" if run.sdk else "",
                    _STATE_TEXT[run.state],
                    _count(run),
                    _count(run, "positive"),
                    _count(run, "negative"),
                ]
                for run in sent
            ],
        ),
        "",
        "## Not sent",
        "",
        *_table(
            ["Operation", "operationId", "Reason"],
            [[_operation_name(run), run.operation_id, run.reason] for run in not_sent],
        ),
        "",
        "## Test cases for each operation",
        "",
    ]
    for run in sent:
        counter = by_label.get(f"{run.method} {run.path}", Counter())
        lines += [
            f"### {_operation_name(run)} — {run.operation_id}",
            "",
            *_table(
                ["Mode", "What the case sends", "Cases"],
                [[mode, what, count] for (mode, what), count in sorted(counter.items())],
            ),
            "",
        ]
    return "\n".join(lines) + "\n"


def write_report(results: list[OperationResult], meta: dict[str, Any], out_dir: Path) -> None:
    (out_dir / "report.md").write_text(render_report(results, meta), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(report_json(results, meta), indent=2), encoding="utf-8"
    )
