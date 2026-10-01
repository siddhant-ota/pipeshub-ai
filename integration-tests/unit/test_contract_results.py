"""How a run is judged: what counts as a difference between the spec and the API,
when two differences are the same one, and what verdict an operation gets."""

from __future__ import annotations

from pathlib import Path

import pytest
from contract_samples import case_event, operation_run, write_events

from helper.contract.baseline import BaselineError, load_baseline, write_baseline
from helper.contract.config import (
    STATE_DESELECTED,
    STATE_NEGATIVE_ONLY,
    STATE_SKIPPED,
    STATE_UNAVAILABLE,
    STATE_VALUE_MISSING,
)
from helper.contract.report import render_index, render_report, summary_lines
from helper.contract.results import (
    NEGATIVE_REJECTION,
    POSITIVE_ACCEPTANCE,
    RESPONSE_SCHEMA,
    STATUS_CODE,
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
    FindingKey,
    OperationResult,
    collect,
)
from helper.contract.sources import FixtureRow
from helper.contract.values import ContractValues

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


def _schema_failure(validator_message: str, where: str, schema: str, value: str) -> str:
    """A `response_schema_conformance` message the way Schemathesis lays it out."""
    title = f"Schema at {where}" if where else "Schema"
    return f"{validator_message}\n\n{title}:\n\n{schema}\n\nValue:\n\n    {value}"


def test_a_schema_difference_is_identified_by_the_rule_not_by_the_value(tmp_path: Path) -> None:
    def wrong_type(case_id: str, value: str) -> dict:
        message = _schema_failure(
            f'{value} is not of type "string"',
            "/components/schemas/Thing/properties/id",
            '    {\n        "type": "string",\n        "description": "The id."\n    }',
            value,
        )
        return case_event(LIST, case_id=case_id, failed={RESPONSE_SCHEMA: message})

    result = _collect(tmp_path, [wrong_type("c1", "null"), wrong_type("c2", '["a","b"]')])

    assert list(result.findings) == [
        FindingKey(
            "listThings",
            RESPONSE_SCHEMA,
            "status 200 /components/schemas/Thing/properties/id",
            'type "string"',
        )
    ]
    (finding,) = result.findings.values()
    assert finding.count == 2
    assert finding.summary.endswith('the response does not satisfy `type "string"`')


@pytest.mark.parametrize(
    ("validator_message", "schema", "expected"),
    [
        # The messages are the validator's (jsonschema_rs); each quotes a value from the response.
        (
            '"abcdef" is longer than 2 characters',
            '    {\n        "maxLength": 2\n    }',
            "maxLength 2",
        ),
        ("[1,2,3] has more than 1 item", '    {\n        "maxItems": 1,\n    }', "maxItems 1"),
        (
            '"the answer is not known" is not one of "a" or "b"',
            '    {\n        "enum": [\n            "a",\n            "b"\n        ]\n    }',
            "enum",
        ),
        (
            '{"a":" is not "} is not valid under any of the schemas listed in the \'anyOf\' keyword',
            '    {\n        "anyOf": [\n            {\n                "type": "string"\n            }\n        ]\n    }',
            "anyOf",
        ),
        ('False schema does not allow "secret"', "    false", "false"),
        # These two name a property of the spec, and the property is the difference.
        (
            '"items" is a required property',
            '    {\n        "required": [\n            "items"\n        ]\n    }',
            '"items" is a required property',
        ),
        (
            "Additional properties are not allowed ('status' was unexpected)",
            '    {\n        "additionalProperties": false,\n        "type": "object"\n    }',
            "Additional properties are not allowed ('status' was unexpected)",
        ),
    ],
)
def test_the_rule_of_a_schema_difference_holds_no_response_value(
    tmp_path: Path, validator_message: str, schema: str, expected: str
) -> None:
    message = _schema_failure(validator_message, "/components/schemas/Thing", schema, "...")

    (key,) = _collect(
        tmp_path, [case_event(LIST, case_id="c1", failed={RESPONSE_SCHEMA: message})]
    ).findings

    assert key.subject == "status 200 /components/schemas/Thing"
    assert key.detail == expected


def test_a_difference_at_the_top_of_the_response_schema(tmp_path: Path) -> None:
    message = _schema_failure(
        '"items" is a required property\n\nValidated against the response schema for status code 200.',
        "",
        '    {\n        "required": [\n            "items"\n        ]\n    }',
        "{}",
    )

    (key,) = _collect(
        tmp_path, [case_event(LIST, case_id="c1", failed={RESPONSE_SCHEMA: message})]
    ).findings

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
    """`body.toolsets[*].instanceId` has no fixture, so its value is random and no toolset has it."""
    run = operation_run(
        "createThing",
        "POST",
        "/things",
        fixtureless_fields=("body.toolsets[*].instanceId", "query.runId"),
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

    # A field whose value the client may choose is not in the list: its request is judged.
    client_chosen = _collect(
        tmp_path, [with_run_id], run=operation_run("createThing", "POST", "/things")
    )
    assert len(client_chosen.findings) == 1


def test_no_2xx_response_leaves_the_operation_unverified(tmp_path: Path) -> None:
    not_found = case_event(LIST, case_id="c1", status=404, passed=(STATUS_CODE,))

    result = _collect(tmp_path, [not_found])

    assert result.verdict == VERDICT_UNVERIFIED
    assert "No request got a 2xx or 3xx response" in result.gap


def test_a_redirect_is_the_success_of_an_operation_that_documents_one(tmp_path: Path) -> None:
    redirect = case_event(LIST, case_id="c1", status=302, passed=(STATUS_CODE,))

    assert _collect(tmp_path, [redirect]).verdict == VERDICT_MATCH


def test_a_baseline_does_not_excuse_an_unverified_operation(tmp_path: Path) -> None:
    """Known differences are an expected failure only if the operation was really exercised."""
    rejected_wrongly = case_event(
        LIST,
        case_id="c1",
        status=404,
        mode="negative",
        query="limit=101",
        data={
            "description": "Value greater than maximum",
            "parameter": "limit",
            "parameter_location": "query",
        },
        failed={STATUS_CODE: "Received: 404\nDocumented: 200, 400"},
    )
    known = {FindingKey("listThings", STATUS_CODE, "status 404", "")}

    result = _collect(tmp_path, [rejected_wrongly], baseline=known)

    assert result.verdict == VERDICT_UNVERIFIED


def test_invalid_requests_that_never_reach_validation_prove_nothing(tmp_path: Path) -> None:
    """A stale ID answers 404 to everything; Schemathesis counts that as "rejected"."""
    run = operation_run(
        "listThings", "GET", "/things", state=STATE_NEGATIVE_ONLY, reason="Calls the LLM."
    )
    not_found = case_event(
        LIST, case_id="c1", status=404, mode="negative", passed=(NEGATIVE_REJECTION,)
    )

    result = _collect(tmp_path, [not_found], run=run)

    assert result.verdict == VERDICT_UNVERIFIED
    assert "No request was rejected as invalid (HTTP 400 or 422)" in result.gap


def test_a_success_where_the_suite_says_there_is_none(tmp_path: Path) -> None:
    run = operation_run("listThings", "GET", "/things", no_success_reason="Nothing to cancel.")

    result = _collect(tmp_path, [VALID], run=run)

    assert result.verdict == VERDICT_STALE_SUITE
    assert "Remove it from `no_success_response`" in result.gap


def test_a_request_without_a_response_makes_the_operation_incomplete(tmp_path: Path) -> None:
    no_response = case_event(LIST, case_id="c2", status=None)

    result = _collect(tmp_path, [VALID, no_response], baseline={LIMIT_KEY})

    assert result.verdict == VERDICT_INCOMPLETE
    assert dict(result.statuses) == {"200": 1, "no response": 1}
    # The difference in the baseline did not show, but the run was cut short: it is not stale.
    assert not result.stale
    assert "did not complete" in result.gap


def test_a_rate_limited_request_is_not_judged(tmp_path: Path) -> None:
    """Schemathesis takes a 429 as "rejected", so an invalid request it hides would pass."""
    limited = case_event(
        LIST, case_id="c2", status=429, mode="negative", passed=(NEGATIVE_REJECTION,)
    )

    result = _collect(tmp_path, [VALID, limited], baseline={LIMIT_KEY})

    assert result.verdict == VERDICT_INCOMPLETE
    assert result.rate_limited == 1
    assert not result.findings and not result.stale
    assert "turned away by a rate limit" in result.gap
    assert "`operation_rate_limits`" in result.gap


def test_a_scenario_that_schemathesis_could_not_finish_makes_the_operation_incomplete(
    tmp_path: Path,
) -> None:
    errored = case_event(LIST, case_id="ok", scenario_status="error", passed=(STATUS_CODE,))

    result = _collect(tmp_path, [errored], baseline={LIMIT_KEY})

    assert result.verdict == VERDICT_INCOMPLETE
    assert not result.stale

    path = tmp_path / "baseline.json"
    path.write_text(
        '{"format_version": 1, "findings": [{"operation": "listThings", '
        f'"check": "{LIMIT_KEY.check}", "subject": "{LIMIT_KEY.subject}", '
        f'"detail": "{LIMIT_KEY.detail}"}}]}}',
        encoding="utf-8",
    )
    assert write_baseline(path, [result]) == (0, 0), "an incomplete run removes nothing"


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
        (STATE_UNAVAILABLE, VERDICT_UNAVAILABLE),
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

    operations = {"listThings", "createThing"}
    assert write_baseline(path, [found]) == (1, 0)
    assert load_baseline(path, operations) == {LIMIT_KEY}

    # A person adds a ticket to the entry, and another operation has an entry of its own.
    document = path.read_text(encoding="utf-8").replace(
        '"detail": "Value greater than maximum"',
        '"detail": "Value greater than maximum",\n      "ticket": "PA-1"',
    )
    other = '{"operation": "createThing", "check": "x", "subject": "y", "detail": "z"}'
    path.write_text(document.replace('"findings": [', f'"findings": [{other},'), encoding="utf-8")

    assert write_baseline(path, [found]) == (0, 0)
    assert '"ticket": "PA-1"' in path.read_text(encoding="utf-8")
    assert len(load_baseline(path, operations)) == 2, (
        "an operation that did not run keeps its entries"
    )

    fixed = _collect(tmp_path, [VALID])
    assert write_baseline(path, [fixed]) == (0, 1)
    assert load_baseline(path, operations) == {FindingKey("createThing", "x", "y", "z")}

    # The suite no longer has `createThing`: its entry could never be found again or go stale.
    with pytest.raises(BaselineError, match="operations that are not in the suite: createThing"):
        load_baseline(path, {"listThings"})


def test_a_baseline_entry_must_say_what_it_is_about(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text('{"format_version": 1, "findings": [{"operation": "listThings"}]}')

    with pytest.raises(BaselineError, match="needs operation, check and subject"):
        load_baseline(path, {"listThings"})

    path.write_text('{"format_version": 2, "findings": []}')
    with pytest.raises(BaselineError, match="unsupported baseline format version 2"):
        load_baseline(path, {"listThings"})


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
    rejected = case_event(
        LIST, case_id="c2", status=400, mode="negative", passed=(NEGATIVE_REJECTION,)
    )
    result = _collect(tmp_path, [LIMIT_ABOVE_MAXIMUM, rejected], run=run)

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
    assert "## Fixtures" not in report


def test_report_says_where_the_values_came_from(tmp_path: Path) -> None:
    values = ContractValues(
        values={"thing.id": "T-1", "thing.token": "do-not-print"},
        missing={"project.id": "fixture `contract_project` failed: HTTP 500"},
    )
    rows = [
        FixtureRow(
            "contract_things", ("thing.id",), "One thing.", True, operations=("deleteThing",)
        ),
        FixtureRow("thing_token", ("thing.token",), "A token.", False, secret=True),
        FixtureRow("contract_project", ("project.id",), "One project.", True),
    ]

    report = render_report(
        [_collect(tmp_path, [VALID])], META, [row.with_values(values) for row in rows]
    )

    assert "| `contract_things` | added | One thing. | `thing.id` = `T-1` | 1 |" in report
    assert "| `thing_token` | existing | A token. | `thing.token` = `(secret)` | 0 |" in report
    assert "**no value** — fixture `contract_project` failed: HTTP 500" in report
    assert "do-not-print" not in report


def test_index_adds_up_the_suites(tmp_path: Path) -> None:
    other = {**META, "suite": "others"}
    clean = _collect(tmp_path, [VALID])
    differs = _collect(tmp_path, [VALID, LIMIT_ABOVE_MAXIMUM])

    index = render_index(
        [(META, [clean], tmp_path / "things.md"), (other, [differs], tmp_path / "others.md")]
    )

    assert "**Contract: DIFFERS** — 1 new difference(s)" in index
    assert f"| [things]({tmp_path / 'things.md'}) | now | 1 |" in index
    assert index.index("[others]") < index.index("[things]")
