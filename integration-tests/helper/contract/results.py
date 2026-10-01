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
from helper.contract.events import Case, read_scenarios
from helper.contract.fields import ANY_ITEM, field_name, pointer_path
from helper.contract.values import QUERY, Substitution, substitute

STATUS_CODE = "status_code_conformance"
CONTENT_TYPE = "content_type_conformance"
RESPONSE_SCHEMA = "response_schema_conformance"
POSITIVE_ACCEPTANCE = "positive_data_acceptance"
NEGATIVE_REJECTION = "negative_data_rejection"
REQUEST_CHECKS = frozenset({POSITIVE_ACCEPTANCE, NEGATIVE_REJECTION})

# The spec and the API agree, and a success response was checked.
VERDICT_MATCH = "match"
# Differences found; the baseline lists every one.
VERDICT_KNOWN_MISMATCH = "known_mismatch"
# A difference that the baseline does not list.
VERDICT_MISMATCH = "mismatch"
# The baseline lists a difference that no longer occurs.
VERDICT_STALE_BASELINE = "stale_baseline"
# The suite file says no request can succeed, and one did.
VERDICT_STALE_SUITE = "stale_suite"
# No difference found; by design the success response was not checked.
VERDICT_PARTIAL = "partial"
# No difference found, but the responses that would show one never came.
VERDICT_UNVERIFIED = "unverified"
# Schemathesis could not finish the operation, so nothing can be said about it.
VERDICT_INCOMPLETE = "incomplete"
# No request was sent because something the operation needs is missing.
VERDICT_NOT_RUN = "not_run"
VERDICT_SKIPPED = "skipped"
VERDICT_DESELECTED = "deselected"

_UNEXPECTED_PROPERTIES = "object_unexpected_properties"
# OpenAPI has no way to forbid a parameter it does not list in these locations,
# so "the API accepted an unknown one" is not a difference from the spec.
_OPEN_LOCATIONS = frozenset({"query", "header", "cookie"})
# What a request that fails validation is answered with.
_VALIDATION_STATUSES = ("400", "422")
_RATE_LIMITED = 429

_SCHEMA_TITLE = re.compile(r"^Schema(?: at (?P<path>\S+?))?:$")
_SCHEMA_KEYWORD = re.compile(r'^\s*"(?P<keyword>[^"]+)":\s*(?P<value>.*?),?\s*$')
_RECEIVED = re.compile(r"^Received: (?P<value>.+)$", re.MULTILINE)
# Keywords whose message names what the spec lacks or demands, and no response value.
_NAMED_BY_MESSAGE = frozenset({"required", "additionalProperties"})


@dataclass(frozen=True)
class FindingKey:
    """What makes two findings the same one, across cases and across runs."""

    operation_id: str
    check: str
    # The part of the contract: `query.page`, `body.filters.kb[*]`, `status 200 /components/schemas/X`.
    subject: str
    # What about it: `Value greater than maximum`, `type "string"`.
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
            return (
                f"{self.key.subject}: {self.key.detail} — the spec forbids it, "
                f"the API accepted it (HTTP {self.example_status})"
            )
        if self.key.check == POSITIVE_ACCEPTANCE:
            return (
                f"{self.key.subject}: {self.key.detail} — the spec allows it, "
                f"the API rejected it (HTTP {self.example_status})"
            )
        if self.key.check == STATUS_CODE:
            return f"HTTP {self.example_status} is not in the spec for this operation"
        if self.key.check == CONTENT_TYPE:
            return f"{self.key.subject}: Content-Type {self.key.detail} is not in the spec"
        return f"{self.key.subject}: the response does not satisfy `{self.key.detail}`"


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
    # Requests without a response or turned away by a rate limit, and scenarios
    # Schemathesis could not finish.
    unfinished: int = 0
    rate_limited: int = 0
    # Failed checks that say nothing about the spec; see `_is_spec_statement`.
    ignored: int = 0
    # Rejected valid requests that named something that does not exist; see `_names_nothing_real`.
    unjudged: int = 0

    @property
    def success_responses(self) -> int:
        return sum(count for status, count in self.statuses.items() if status.startswith("2"))

    @property
    def validation_rejections(self) -> int:
        return sum(self.statuses[status] for status in _VALIDATION_STATUSES)

    @property
    def new_findings(self) -> list[Finding]:
        return [finding for finding in self.findings.values() if not finding.known]

    @property
    def known_findings(self) -> list[Finding]:
        return [finding for finding in self.findings.values() if finding.known]

    @property
    def _declares_no_success(self) -> bool:
        return self.run.state == STATE_NEGATIVE_ONLY or bool(self.run.no_success_reason)

    @property
    def _unexpected_success(self) -> bool:
        return bool(self.run.no_success_reason) and self.success_responses > 0

    @property
    def _missing_evidence(self) -> str:
        """Why the responses do not show that the operation was really exercised, or ""."""
        if self.run.state == STATE_NEGATIVE_ONLY:
            # Schemathesis also takes 401, 403 and 404 as "rejected". If every answer is one
            # of those, a stale ID or the login turned the requests away, not the validation.
            if self.validation_rejections:
                return ""
            return (
                "No request was rejected as invalid (HTTP 400 or 422), so nothing shows "
                "that the invalid requests reached the validation of this operation."
            )
        if self.run.no_success_reason or self.success_responses:
            return ""
        return (
            "No request got a 2xx response, so the success response was not checked "
            "against the spec. Give the operation valid values, or declare it under "
            "`no_success_response` in the suite file."
        )

    @property
    def verdict(self) -> str:
        if self.run.state == STATE_DESELECTED:
            return VERDICT_DESELECTED
        if self.run.state == STATE_SKIPPED:
            return VERDICT_SKIPPED
        if not self.run.is_sent or not self.cases:
            return VERDICT_NOT_RUN
        if self.unfinished:
            return VERDICT_INCOMPLETE
        if self.new_findings:
            return VERDICT_MISMATCH
        if self.stale:
            return VERDICT_STALE_BASELINE
        # Before "known": the baseline must not turn an unchecked operation into an expected failure.
        if self._unexpected_success:
            return VERDICT_STALE_SUITE
        if self._missing_evidence:
            return VERDICT_UNVERIFIED
        if self.findings:
            return VERDICT_KNOWN_MISMATCH
        return VERDICT_PARTIAL if self._declares_no_success else VERDICT_MATCH

    @property
    def gap(self) -> str:
        """Why the operation is not fully verified, or "" if it is."""
        if not self.run.is_sent:
            return self.run.reason
        if not self.cases:
            return "Schemathesis sent no test case for this operation."
        if self.rate_limited:
            return (
                f"{self.rate_limited} request(s) were turned away by a rate limit (HTTP 429) and "
                "so were not checked. Set a lower `rate_limit`, or one under "
                "`operation_rate_limits`, in the suite file."
            )
        if self.unfinished:
            return (
                f"{self.unfinished} request(s) or scenario(s) did not complete (no response, "
                "or Schemathesis reported an error), so the result is not reliable."
            )
        if self._unexpected_success:
            return (
                "The suite file says no request to this operation can succeed, but "
                f"{self.success_responses} did. Remove it from `no_success_response`."
            )
        if self._missing_evidence:
            return self._missing_evidence
        if self.run.state == STATE_NEGATIVE_ONLY:
            return f"Invalid requests only; the success response is not checked. {self.run.reason}"
        if self.run.no_success_reason:
            return f"No success response is expected. {self.run.no_success_reason}"
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
    named = [segment for segment in pointer_path(case.schema_pointer) if segment != ANY_ITEM]
    for name in (case.parameter, named[-1] if named else ""):
        if name:
            description = description.removeprefix(f"{name}: ")
    return description


def _schema_difference(message: str) -> tuple[str, str]:
    """(where in the spec, which rule) for a response that does not match its schema.

    Schemathesis writes the message as

        <validator message>

        Schema at /components/schemas/Thing/properties/id:

            {
                "type": "string",
                ...

    with the rule that failed first in the schema. The validator message quotes
    the value from the response, which changes from run to run, so the rule is
    read from the schema instead. `required` and `additionalProperties` are the
    exception: their message names the property, and the property is the difference.
    """
    lines = message.splitlines()
    validator_message = next((line.strip() for line in lines if line.strip()), "")
    for index, line in enumerate(lines):
        title = _SCHEMA_TITLE.match(line.strip())
        if not title:
            continue
        where = title["path"] or "response body"
        for schema_line in lines[index + 1 :]:
            keyword = _SCHEMA_KEYWORD.match(schema_line)
            if keyword:
                if keyword["keyword"] in _NAMED_BY_MESSAGE:
                    return where, validator_message
                value = keyword["value"]
                # A value that continues on the next lines is a list or an object.
                if value and value[-1] not in "[{":
                    return where, f"{keyword['keyword']} {value}"
                return where, keyword["keyword"]
            if schema_line.strip() not in ("", "{"):
                # The schema is `true`/`false` or not an object: there is no rule to name.
                return where, schema_line.strip()
        return where, ""
    return "response body", validator_message


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
    where, rule = _schema_difference(message)
    return FindingKey(operation_id, check, f"{status} {where}", rule)


def _is_spec_statement(case: Case, check: str) -> bool:
    return not (
        check == NEGATIVE_REJECTION
        and case.scenario == _UNEXPECTED_PROPERTIES
        and case.location in _OPEN_LOCATIONS
    )


def _names_nothing_real(case: Case, fixtureless_fields: tuple[str, ...]) -> bool:
    """True if the request has a generated value where the ID of an existing thing belongs.

    `fixtureless_fields` are the suite's `ids_without_fixture`: a toolset, an
    attachment and the like. The spec allows such a request, but nothing has
    that ID and the API is right to reject it, so the rejection says nothing
    about the spec. A field whose value the client is free to choose is not in
    this list: a rejection of that request is judged like any other.
    """
    for name in fixtureless_fields:
        probe = Substitution.for_field(name, "")
        if probe.location == QUERY:
            if probe.path[0] in case.query:
                return True
        elif substitute(case.request_data(), probe)[1]:
            return True
    return False


def _example_request(case: Case) -> str:
    body = case.request_body
    return f"{case.method} {case.target} {body}".rstrip()


def _record(result: OperationResult, case: Case, baseline: set[FindingKey]) -> None:
    result.cases += 1
    if case.is_negative:
        result.negative += 1
    else:
        result.positive += 1
    if case.status is None:
        result.unfinished += 1
        result.statuses["no response"] += 1
        return
    result.statuses[str(case.status)] += 1
    if case.status == _RATE_LIMITED:
        # The limiter answered, not the operation, so the request says nothing about the spec.
        # Schemathesis takes a 429 as "accepted" and as "rejected" alike.
        result.rate_limited += 1
        result.unfinished += 1
        return
    for check in case.checks:
        if check.passed:
            continue
        if not _is_spec_statement(case, check.name):
            result.ignored += 1
            continue
        if check.name == POSITIVE_ACCEPTANCE and _names_nothing_real(
            case, result.run.fixtureless_fields
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


def collect(
    runs: list[OperationRun],
    ndjson_path: Path | None,
    baseline: set[FindingKey] | None = None,
) -> list[OperationResult]:
    """Group the cases of a run by operation and mark each finding as known or new."""
    baseline = baseline or set()
    results = {run.label: OperationResult(run) for run in runs}
    if ndjson_path is not None and ndjson_path.exists():
        for scenario in read_scenarios(ndjson_path):
            result = results.get(scenario.label)
            if result is None:
                continue
            if not scenario.completed:
                result.unfinished += 1
            for case in scenario.cases:
                _record(result, case, baseline)

    for result in results.values():
        # Only a complete run of the operation can show that a difference is gone.
        if result.run.is_sent and result.cases and not result.unfinished:
            result.stale = sorted(
                (
                    key
                    for key in baseline
                    if key.operation_id == result.run.operation_id and key not in result.findings
                ),
                key=str,
            )
    return list(results.values())
