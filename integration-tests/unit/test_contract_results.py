"""How a run is judged: what counts as a difference between the spec and the API,
when two differences are the same one, and what verdict an operation gets."""

from __future__ import annotations

from pathlib import Path

import pytest
from contract_samples import case_event, operation_run, write_events

from helper.contract.baseline import load_baseline, write_baseline
from helper.contract.config import (
    STATE_DESELECTED,
    STATE_NEGATIVE_ONLY,
    STATE_SKIPPED,
    STATE_VALUE_MISSING,
)
from helper.contract.report import render_report, summary_lines
from helper.contract.results import (
    NEGATIVE_REJECTION,
    POSITIVE_ACCEPTANCE,
    RESPONSE_SCHEMA,
    STATUS_CODE,
    VERDICT_DESELECTED,
    VERDICT_KNOWN_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NOT_RUN,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED,
    VERDICT_STALE_BASELINE,
    VERDICT_UNVERIFIED,
    FindingKey,
    OperationResult,
    collect,
)

pytestmark = pytest.mark.unit

LIST = "GET /things"
RUN = operation_run("listThings", "GET", "/things")
META = {
    "suite": "things",
    "target": "http://localhost/api/v1",
    "time": "now",
    "schemathesis": "4",
    "seed": 42,
    "spec_commit": "abc",
}

LIMIT_ABOVE_MAXIMUM = case_event(
    LIST,
    case_id="c1",
    mode="negative",
    query="limit=101",
    data={
        "scenario": "value_above_maximum",
        "description": "limit: Value greater than maximum",
        "parameter": "limit",
        "parameter_location": "query",
    },
    failed={NEGATIVE_REJECTION: "Invalid data should have been rejected"},
)
LIMIT_KEY = FindingKey(
    "listThings", NEGATIVE_REJECTION, "query.limit", "Value greater than maximum"
)
VALID = case_event(LIST, case_id="ok", passed=(STATUS_CODE, RESPONSE_SCHEMA))


def _collect(tmp_path: Path, events: list, run=RUN, baseline=None) -> OperationResult:
    (result,) = collect([run], write_events(tmp_path, events), baseline)
    return result


def test_a_valid_request_that_matches_the_spec_is_a_match(tmp_path: Path) -> None:
    result = _collect(tmp_path, [VALID])

    assert result.verdict == VERDICT_MATCH
    assert (result.cases, result.positive, result.negative) == (1, 1, 0)
    assert not result.gap


def test_an_accepted_invalid_request_is_a_difference(tmp_path: Path) -> None:
    result = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM])

    assert result.verdict == VERDICT_MISMATCH
    (finding,) = result.new_findings
    assert finding.key == LIMIT_KEY
    assert finding.example_request == "GET /things?limit=101"
    assert "the spec forbids it, the API accepted it (HTTP 200)" in finding.summary


def test_cases_that_show_the_same_difference_are_one_finding(tmp_path: Path) -> None:
    again = case_event(
        LIST,
        case_id="c2",
        mode="negative",
        query="limit=5000",
        data={
            "scenario": "value_above_maximum",
            "description": "Value greater than maximum",
            "parameter": "limit",
            "parameter_location": "query",
        },
        failed={NEGATIVE_REJECTION: "Invalid data should have been rejected"},
    )

    result = _collect(tmp_path, [LIMIT_ABOVE_MAXIMUM, again])

    assert [finding.count for finding in result.findings.values()] == [2]


def test_a_rejected_valid_body_names_the_body_field(tmp_path: Path) -> None:
    rejected = case_event(
        "POST /things",
        case_id="c1",
        status=400,
        body={"models": [{"modelKey": "x"}]},
        data={
            "scenario": "enum_value",
            "description": "Enum value",
            "parameter": "application/json",
            "parameter_location": "body",
            "location": "/properties/models/items/properties/provider",
        },
        failed={POSITIVE_ACCEPTANCE: "Valid data should have been accepted"},
    )

    result = _collect(tmp_path, [rejected], run=operation_run("createThing", "POST", "/things"))

    (finding,) = result.findings.values()
    assert finding.key.subject == "body.models[*].provider"
    assert finding.key.detail == "Enum value"
    assert "the spec allows it, the API rejected it (HTTP 400)" in finding.summary
    assert finding.example_request == 'POST /things {"models": [{"modelKey": "x"}]}'


def test_a_body_finding_does_not_repeat_the_field_name(tmp_path: Path) -> None:
    accepted = case_event(
        "POST /things",
        case_id="c1",
        status=201,
        mode="negative",
        body={"name": None},
        data={
            "scenario": "incorrect_type",
            "description": "name: Incorrect type",
            "parameter": "application/json",
            "parameter_location": "body",
            "location": "/properties/name/type",
        },
        failed={NEGATIVE_REJECTION: "Invalid data should have been rejected"},
    )

    (key,) = _collect(
        tmp_path, [accepted], run=operation_run("createThing", "POST", "/things")
    ).findings

    assert (key.subject, key.detail) == ("body.name", "Incorrect type")


def test_a_schema_difference_is_identified_by_the_rule_not_by_the_value(tmp_path: Path) -> None:
    def wrong_type(case_id: str, value: str) -> dict:
        return case_event(
            LIST,
            case_id=case_id,
            failed={
                RESPONSE_SCHEMA: (
                    f'{value} is not of type "string"\n\n'
                    "Schema at /components/schemas/Thing/properties/id:\n\n    {}\n"
                )
            },
        )

    result = _collect(tmp_path, [wrong_type("c1", "null"), wrong_type("c2", '["a","b"]')])

    assert list(result.findings) == [
        FindingKey(
            "listThings",
            RESPONSE_SCHEMA,
            "status 200 /components/schemas/Thing/properties/id",
            'is not of type "string"',
        )
    ]


def test_a_missing_required_property_keeps_its_name(tmp_path: Path) -> None:
    missing = case_event(
        LIST,
        case_id="c1",
        failed={
            RESPONSE_SCHEMA: '"items" is a required property\n\nValidated against the response schema'
        },
    )

    (key,) = _collect(tmp_path, [missing]).findings

    assert (key.subject, key.detail) == (
        "status 200 response body",
        '"items" is a required property',
    )


def test_an_undocumented_status_code_is_a_difference(tmp_path: Path) -> None:
    undocumented = case_event(
        LIST,
        case_id="c1",
        status=500,
        failed={STATUS_CODE: "Received: 500\nDocumented: 200, 400"},
    )

    result = _collect(tmp_path, [VALID, undocumented])

    (finding,) = result.findings.values()
    assert finding.key == FindingKey("listThings", STATUS_CODE, "status 500", "")
    assert finding.summary == "HTTP 500 is not in the spec for this operation"


def test_an_unknown_query_parameter_is_not_a_spec_difference(tmp_path: Path) -> None:
    unknown = case_event(
        LIST,
        case_id="c1",
        mode="negative",
        query="x-schemathesis-unknown-property=42",
        data={
            "scenario": "object_unexpected_properties",
            "description": "Object with unexpected properties",
            "parameter_location": "query",
        },
        failed={NEGATIVE_REJECTION: "Invalid data should have been rejected"},
    )

    result = _collect(tmp_path, [VALID, unknown])

    assert result.verdict == VERDICT_MATCH
    assert result.ignored == 1


def test_an_unknown_body_property_is_a_spec_difference(tmp_path: Path) -> None:
    """`additionalProperties: false` in a body schema is a statement the spec does make."""
    unknown = case_event(
        "POST /things",
        case_id="c1",
        mode="negative",
        body={"x-schemathesis-unknown-property": 42},
        data={
            "scenario": "object_unexpected_properties",
            "description": "Object with unexpected properties",
            "parameter": "application/json",
            "parameter_location": "body",
            "location": "/additionalProperties",
        },
        failed={NEGATIVE_REJECTION: "Invalid data should have been rejected"},
    )

    result = _collect(tmp_path, [unknown], run=operation_run("createThing", "POST", "/things"))

    assert result.verdict == VERDICT_MISMATCH


def test_a_rejected_request_that_names_nothing_real_is_not_judged(tmp_path: Path) -> None:
    """The suite waives `body.toolsets[*].instanceId`, so its value is random and no toolset has it."""
    run = operation_run(
        "createThing",
        "POST",
        "/things",
        waived_fields=("body.toolsets[*].instanceId", "query.runId"),
    )
    failed = {POSITIVE_ACCEPTANCE: "Valid data should have been accepted"}
    data = {
        "description": "Maximum length",
        "parameter": "application/json",
        "parameter_location": "body",
        "location": "/properties/name",
    }

    with_toolset = case_event(
        "POST /things",
        case_id="a",
        status=400,
        body={"name": "n", "toolsets": [{"instanceId": "random"}]},
        data=data,
        failed=failed,
    )
    with_run_id = case_event(
        "POST /things",
        case_id="b",
        status=400,
        query="runId=random",
        body={"name": "n"},
        data=data,
        failed=failed,
    )
    plain = case_event(
        "POST /things",
        case_id="c",
        status=400,
        body={"name": "n", "toolsets": []},
        data=data,
        failed=failed,
    )

    unjudged = _collect(tmp_path, [with_toolset, with_run_id], run=run)
    assert not unjudged.findings
    assert unjudged.unjudged == 2

    judged = _collect(tmp_path, [plain], run=run)
    assert [key.subject for key in judged.findings] == ["body.name"]
    assert judged.unjudged == 0


def test_no_2xx_response_leaves_the_operation_unverified(tmp_path: Path) -> None:
    not_found = case_event(LIST, case_id="c1", status=404, passed=(STATUS_CODE,))

    result = _collect(tmp_path, [not_found])

    assert result.verdict == VERDICT_UNVERIFIED
    assert "No request got a 2xx response" in result.gap


def test_a_declared_gap_is_partial_not_a_failure(tmp_path: Path) -> None:
    rejected = case_event(
        LIST, case_id="c1", status=400, mode="negative", passed=(NEGATIVE_REJECTION,)
    )

    negative_only = _collect(
        tmp_path,
        [rejected],
        run=operation_run(
            "listThings", "GET", "/things", state=STATE_NEGATIVE_ONLY, reason="Calls the LLM."
        ),
    )
    no_success = _collect(
        tmp_path,
        [rejected],
        run=operation_run("listThings", "GET", "/things", no_success_reason="Nothing to cancel."),
    )

    assert negative_only.verdict == no_success.verdict == VERDICT_PARTIAL
    assert "Calls the LLM." in negative_only.gap
    assert "Nothing to cancel." in no_success.gap


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        (STATE_VALUE_MISSING, VERDICT_NOT_RUN),
        (STATE_SKIPPED, VERDICT_SKIPPED),
        (STATE_DESELECTED, VERDICT_DESELECTED),
    ],
)
def test_an_operation_that_was_not_sent(tmp_path: Path, state: str, verdict: str) -> None:
    result = _collect(
        tmp_path,
        [],
        run=operation_run("listThings", "GET", "/things", state=state, reason="Because."),
    )

    assert result.verdict == verdict
    assert result.gap == "Because."


def test_an_operation_that_was_sent_but_has_no_case_is_not_run(tmp_path: Path) -> None:
    result = _collect(tmp_path, [])

    assert result.verdict == VERDICT_NOT_RUN
    assert "sent no test case" in result.gap


def test_a_baselined_difference_is_known(tmp_path: Path) -> None:
    result = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM], baseline={LIMIT_KEY})

    assert result.verdict == VERDICT_KNOWN_MISMATCH
    assert not result.new_findings and len(result.known_findings) == 1


def test_a_new_difference_fails_even_next_to_a_known_one(tmp_path: Path) -> None:
    other = FindingKey("listThings", NEGATIVE_REJECTION, "query.page", "Value greater than maximum")

    result = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM], baseline={other, LIMIT_KEY})
    assert result.verdict == VERDICT_STALE_BASELINE
    assert result.stale == [other]

    result = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM], baseline={other})
    assert result.verdict == VERDICT_MISMATCH


def test_a_baseline_entry_is_not_stale_when_its_operation_did_not_run(tmp_path: Path) -> None:
    result = _collect(
        tmp_path,
        [],
        run=operation_run("listThings", "GET", "/things", state=STATE_VALUE_MISSING),
        baseline={LIMIT_KEY},
    )

    assert not result.stale


def test_baseline_round_trip_keeps_notes_and_drops_what_is_fixed(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    found = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM])

    assert write_baseline(path, [found]) == (1, 0)
    assert load_baseline(path) == {LIMIT_KEY}

    # A person adds a ticket to the entry, and another operation has an entry of its own.
    document = path.read_text(encoding="utf-8").replace(
        '"detail": "Value greater than maximum"',
        '"detail": "Value greater than maximum",\n      "ticket": "PA-1"',
    )
    other = '{"operation": "createThing", "check": "x", "subject": "y", "detail": "z"}'
    path.write_text(document.replace('"findings": [', f'"findings": [{other},'), encoding="utf-8")

    assert write_baseline(path, [found]) == (0, 0)
    assert '"ticket": "PA-1"' in path.read_text(encoding="utf-8")
    assert len(load_baseline(path)) == 2, "an operation that did not run keeps its entries"

    fixed = _collect(tmp_path, [VALID])
    assert write_baseline(path, [fixed]) == (0, 1)
    assert load_baseline(path) == {FindingKey("createThing", "x", "y", "z")}


def test_report_states_the_contract_and_the_coverage(tmp_path: Path) -> None:
    differs = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM])
    skipped = OperationResult(
        operation_run(
            "deleteThing", "DELETE", "/things/{thingId}", state=STATE_SKIPPED, reason="Destructive."
        )
    )

    report = render_report([differs, skipped], META)

    assert (
        "**Contract: DIFFERS** — 1 new difference(s), 0 known, 0 stale in the baseline." in report
    )
    assert "**Coverage: INCOMPLETE** — 1 of 2 operations have a coverage gap." in report
    assert "| `DELETE /things/{thingId}` | deleteThing | Skipped | Destructive. |" in report
    assert "- **new** — query.limit: Value greater than maximum" in report
    assert "Example: `GET /things?limit=101`" in report


def test_an_operation_with_differences_can_also_have_a_coverage_gap(tmp_path: Path) -> None:
    """A negative-only operation that differs is still not fully checked."""
    run = operation_run(
        "listThings", "GET", "/things", state=STATE_NEGATIVE_ONLY, reason="Calls the LLM."
    )
    result = _collect(tmp_path, [LIMIT_ABOVE_MAXIMUM], run=run)

    assert result.verdict == VERDICT_MISMATCH
    assert summary_lines([result]) == [
        "Contract: DIFFERS — 1 new difference(s), 0 known, 0 stale in the baseline.",
        "Coverage: INCOMPLETE — 1 of 1 operations have a coverage gap.",
        "  Differs: GET /things — Invalid requests only; the success response is not checked. Calls the LLM.",
    ]


def test_report_of_a_clean_run(tmp_path: Path) -> None:
    report = render_report([_collect(tmp_path, [VALID])], META)

    assert "**Contract: MATCHES**" in report
    assert "**Coverage: COMPLETE**" in report
