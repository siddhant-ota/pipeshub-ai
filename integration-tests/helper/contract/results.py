"""What a run found: the differences between the spec and the API, per operation.

The API is the reference. A finding is one way in which the spec says something
else than the API does. It does not say which side is wrong.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from helper.contract.config import (
    STATE_DESELECTED,
    STATE_NEGATIVE_ONLY,
    STATE_SKIPPED,
    OperationRun,
)
from helper.contract.events import Case, read_cases
from helper.contract.fields import ANY_ITEM, field_name, pointer_path
from helper.contract.values import QUERY, Substitution, substitute

STATUS_CODE = "status_code_conformance"
CONTENT_TYPE = "content_type_conformance"
RESPONSE_SCHEMA = "response_schema_conformance"
POSITIVE_ACCEPTANCE = "positive_data_acceptance"
NEGATIVE_REJECTION = "negative_data_rejection"
RESPONSE_CHECKS = frozenset({STATUS_CODE, CONTENT_TYPE, RESPONSE_SCHEMA})
REQUEST_CHECKS = frozenset({POSITIVE_ACCEPTANCE, NEGATIVE_REJECTION})

# The spec and the API agree, and a success response was checked.
VERDICT_MATCH = "match"
# Differences found; the baseline lists every one.
VERDICT_KNOWN_MISMATCH = "known_mismatch"
# A difference that the baseline does not list.
VERDICT_MISMATCH = "mismatch"
# The baseline lists a difference that no longer occurs.
VERDICT_STALE_BASELINE = "stale_baseline"
# No difference found, but by design no success response was checked.
VERDICT_PARTIAL = "partial"
# No difference found, but no 2xx response came back, so the success response is unchecked.
VERDICT_UNVERIFIED = "unverified"
# No request was sent because something the operation needs is missing.
VERDICT_NOT_RUN = "not_run"
VERDICT_SKIPPED = "skipped"
VERDICT_DESELECTED = "deselected"

_UNEXPECTED_PROPERTIES = "object_unexpected_properties"
# OpenAPI has no way to forbid a parameter it does not list in these locations,
# so "the API accepted an unknown one" is not a difference from the spec.
_OPEN_LOCATIONS = frozenset({"query", "header", "cookie"})

_SCHEMA_AT = re.compile(r"^Schema at (?P<path>\S+?):?$", re.MULTILINE)
_RECEIVED = re.compile(r"^Received: (?P<value>.+)$", re.MULTILINE)
# jsonschema puts the offending value first: `["a"] is not of type "string"`.
# The value changes between runs, the rule that failed does not.
_VALUE_PREFIX = re.compile(
    r"^.+? (?=(?:is not |is too |is less than |is greater than |does not |has too |should be |must be ))"
)
_REQUIRED = " is a required property"


@dataclass(frozen=True)
class FindingKey:
    """What makes two findings the same one, across cases and across runs."""

    operation_id: str
    check: str
    # The part of the contract: `query.page`, `body.filters.kb[*]`, `status 200 /components/schemas/X`.
    subject: str
    # What about it: `Value greater than maximum`, `not of type "string"`.
    detail: str

    def __str__(self) -> str:
        return " | ".join(part for part in (self.check, self.subject, self.detail) if part)


@dataclass
class Finding:
    key: FindingKey
    title: str
    count: int = 0
    known: bool = False
    example_request: str = ""
    example_status: int | None = None
    example_message: str = ""

    @property
    def summary(self) -> str:
        if self.key.check == NEGATIVE_REJECTION:
            return f"{self.key.subject}: {self.key.detail} — the spec forbids it, the API accepted it (HTTP {self.example_status})"
        if self.key.check == POSITIVE_ACCEPTANCE:
            return f"{self.key.subject}: {self.key.detail} — the spec allows it, the API rejected it (HTTP {self.example_status})"
        if self.key.check == STATUS_CODE:
            return f"HTTP {self.example_status} is not in the spec for this operation"
        if self.key.check == CONTENT_TYPE:
            return f"{self.key.subject}: Content-Type {self.key.detail} is not in the spec"
        return f"{self.key.subject}: {self.key.detail}"


@dataclass
class OperationResult:
    run: OperationRun
    cases: int = 0
    positive: int = 0
    negative: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    findings: dict[FindingKey, Finding] = field(default_factory=dict)
    # Baseline entries for this operation that the run did not reproduce.
    stale: list[FindingKey] = field(default_factory=list)
    # Failed checks that say nothing about the spec; see `_is_spec_statement`.
    ignored: int = 0
    # Rejected valid requests that named something that does not exist; see `_names_nothing_real`.
    unjudged: int = 0

    @property
    def success_responses(self) -> int:
        return sum(count for status, count in self.statuses.items() if status.startswith("2"))

    @property
    def new_findings(self) -> list[Finding]:
        return [finding for finding in self.findings.values() if not finding.known]

    @property
    def known_findings(self) -> list[Finding]:
        return [finding for finding in self.findings.values() if finding.known]

    @property
    def verdict(self) -> str:
        if self.run.state == STATE_DESELECTED:
            return VERDICT_DESELECTED
        if self.run.state == STATE_SKIPPED:
            return VERDICT_SKIPPED
        if not self.run.is_sent or not self.cases:
            return VERDICT_NOT_RUN
        if self.new_findings:
            return VERDICT_MISMATCH
        if self.stale:
            return VERDICT_STALE_BASELINE
        if self.findings:
            return VERDICT_KNOWN_MISMATCH
        if self.run.state == STATE_NEGATIVE_ONLY or self.run.no_success_reason:
            return VERDICT_PARTIAL
        if not self.success_responses:
            return VERDICT_UNVERIFIED
        return VERDICT_MATCH

    @property
    def gap(self) -> str:
        """Why the operation is not fully verified, or "" if it is."""
        if not self.run.is_sent:
            return self.run.reason
        if not self.cases:
            return "Schemathesis sent no test case for this operation."
        if self.run.state == STATE_NEGATIVE_ONLY:
            return f"Invalid requests only; the success response is not checked. {self.run.reason}"
        if self.run.no_success_reason:
            return f"No success response is expected. {self.run.no_success_reason}"
        if not self.success_responses:
            return (
                "No request got a 2xx response, so the success response was not checked "
                "against the spec. Give the operation valid values, or declare it under "
                "`no_success_response` in the suite file."
            )
        return ""


def _field_name(case: Case) -> str:
    """The request field a case is about, named as in the suite file."""
    if not case.location:
        return "request"
    if case.location != "body":
        return f"{case.location}.{case.parameter}" if case.parameter else case.location
    return field_name("body", pointer_path(case.schema_pointer))


def _description(case: Case) -> str:
    """What the case does to its field, without the field name the subject already gives."""
    description = case.description or "Example from the spec"
    path = pointer_path(case.schema_pointer)
    named = [segment for segment in path if segment != ANY_ITEM]
    for name in (case.parameter, named[-1] if named else ""):
        if name:
            description = description.removeprefix(f"{name}: ")
    return description


def _schema_reason(message: str) -> str:
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if first_line.endswith(_REQUIRED):
        return first_line
    return _VALUE_PREFIX.sub("", first_line, count=1)


def finding_key(operation_id: str, case: Case, check: str, message: str) -> FindingKey:
    if check in REQUEST_CHECKS:
        return FindingKey(operation_id, check, _field_name(case), _description(case))
    status = f"status {case.status}"
    if check == STATUS_CODE:
        return FindingKey(operation_id, check, status, "")
    if check == CONTENT_TYPE:
        received = _RECEIVED.search(message)
        return FindingKey(
            operation_id, check, status, received["value"].strip() if received else ""
        )
    schema = _SCHEMA_AT.search(message)
    subject = f"{status} {schema['path']}" if schema else f"{status} response body"
    return FindingKey(operation_id, check, subject, _schema_reason(message))


def _is_spec_statement(case: Case, check: str) -> bool:
    return not (
        check == NEGATIVE_REJECTION
        and case.scenario == _UNEXPECTED_PROPERTIES
        and case.location in _OPEN_LOCATIONS
    )


def _names_nothing_real(case: Case, waived_fields: tuple[str, ...]) -> bool:
    """True if the request has a generated value in an ID field that the suite waives.

    The spec allows such a request, but it names a toolset, a record or the like
    that does not exist, and the API is right to reject it. Its rejection says
    nothing about the spec.
    """
    for name in waived_fields:
        probe = Substitution.for_field(name, "")
        if probe.location == QUERY:
            if probe.path[0] in case.query:
                return True
        elif substitute(case.request_json(), probe)[1]:
            return True
    return False


def _example_request(case: Case) -> str:
    body = case.request_body
    return f"{case.method} {case.target} {body}".rstrip()


def collect(
    runs: list[OperationRun],
    ndjson_path: Path | None,
    baseline: set[FindingKey] | None = None,
) -> list[OperationResult]:
    """Group the cases of a run by operation and mark each finding as known or new."""
    baseline = baseline or set()
    results = {run.label: OperationResult(run) for run in runs}
    if ndjson_path is not None and ndjson_path.exists():
        for case in read_cases(ndjson_path):
            result = results.get(case.label)
            if result is None:
                continue
            result.cases += 1
            if case.is_negative:
                result.negative += 1
            else:
                result.positive += 1
            result.statuses[str(case.status) if case.status is not None else "no response"] += 1
            for check in case.checks:
                if check.passed:
                    continue
                if not _is_spec_statement(case, check.name):
                    result.ignored += 1
                    continue
                if check.name == POSITIVE_ACCEPTANCE and _names_nothing_real(
                    case, result.run.waived_fields
                ):
                    result.unjudged += 1
                    continue
                key = finding_key(result.run.operation_id, case, check.name, check.message)
                finding = result.findings.setdefault(
                    key,
                    Finding(
                        key=key,
                        title=check.title or check.name,
                        known=key in baseline,
                        example_request=_example_request(case),
                        example_status=case.status,
                        example_message=check.message,
                    ),
                )
                finding.count += 1

    for result in results.values():
        # An operation that sent nothing cannot show that a difference is gone.
        if result.run.is_sent and result.cases:
            result.stale = sorted(
                (
                    key
                    for key in baseline
                    if key.operation_id == result.run.operation_id and key not in result.findings
                ),
                key=str,
            )
    return list(results.values())
