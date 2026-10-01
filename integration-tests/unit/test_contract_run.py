"""What a run does with each operation, and the Schemathesis side of it.

The last tests start Schemathesis against a local stub. They pin how the
installed Schemathesis version generates cases and writes its NDJSON report,
which is everything the contract tests read from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from contract_samples import SPEC, case_event, write_events, write_suite

from helper.contract import runner
from helper.contract.config import (
    STATE_DESELECTED,
    STATE_FULL,
    STATE_NEGATIVE_ONLY,
    STATE_SKIPPED,
    STATE_VALUE_MISSING,
    build_config,
    build_substitutions,
    plan_run,
)
from helper.contract.created import created_resource_paths
from helper.contract.events import read_cases
from helper.contract.suite import Suite, load_suite
from helper.contract.values import ContractValues

pytestmark = pytest.mark.unit

ENTERPRISE_SEARCH = (
    Path(__file__).resolve().parents[1]
    / "response-validation/enterprise-search/contract/suite.yaml"
)
PLANNED = {
    "POST /search": "search",
    "GET /search": "searchHistory",
    "GET /conversations": "getAllConversations",
    "POST /conversations/create": "createConversation",
}
ALL_VALUES = ContractValues(
    values={"thing.id": "T1", "project.id": "P1", "kb.id": "K1", "llm.key": "M1"}
)


@pytest.fixture
def suite(tmp_path: Path) -> Suite:
    return load_suite(
        write_suite(
            tmp_path,
            negative_only={"reason": "Costs money.", "operations": ["createThing"]},
            created_resources=[
                {
                    "operation": "createThing",
                    "id_pointer": "/thing/_id",
                    "delete_path": "/things/{id}?p={project.id}",
                }
            ],
        ),
        SPEC,
    )


def _states(
    suite: Suite, values: ContractValues, selected: set[str] | None = None
) -> dict[str, str]:
    return {run.operation_id: run.state for run in plan_run(suite, values, selected)}


def test_plan_run_with_every_value(suite: Suite) -> None:
    assert _states(suite, ALL_VALUES) == {
        "listThings": STATE_FULL,
        "createThing": STATE_NEGATIVE_ONLY,
        "deleteThing": STATE_FULL,
    }


def test_an_operation_without_one_of_its_values_is_not_sent(suite: Suite) -> None:
    values = ContractValues(
        values={"thing.id": "T1", "kb.id": "K1", "llm.key": "M1"},
        missing={"project.id": "Create project: HTTP 500"},
    )

    runs = {run.operation_id: run for run in plan_run(suite, values)}

    assert runs["listThings"].state == STATE_VALUE_MISSING
    assert runs["listThings"].reason == "Missing value: project.id (Create project: HTTP 500)"
    assert runs["deleteThing"].state == STATE_FULL


def test_a_value_no_fixture_provides_is_named(suite: Suite) -> None:
    runs = {run.operation_id: run for run in plan_run(suite, ContractValues())}

    assert runs["deleteThing"].reason == "Missing value: thing.id (no fixture provides it)"


def test_selection_and_skip(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(tmp_path, skip=[{"operation": "deleteThing", "reason": "Destructive."}]), SPEC
    )

    assert _states(suite, ALL_VALUES, selected={"listThings", "deleteThing"}) == {
        "listThings": STATE_FULL,
        "createThing": STATE_DESELECTED,
        "deleteThing": STATE_SKIPPED,
    }


def test_config_carries_path_values_and_switches_operations_off(suite: Suite) -> None:
    values = ContractValues(values={"thing.id": "T1", "kb.id": "K1", "llm.key": "M1"})
    runs = plan_run(suite, values)

    config = build_config(suite, values, runs)
    blocks = {block["include-operation-id"]: block for block in config["operations"]}

    assert blocks["deleteThing"]["parameters"] == {"path.thingId": "T1"}
    assert blocks["listThings"] == {"include-operation-id": "listThings", "enabled": False}
    assert blocks["createThing"]["generation"] == {"mode": "negative"}
    assert blocks["createThing"]["phases"] == {"examples": {"enabled": False}}
    assert config["hooks"].endswith("helper/contract/hooks.py")
    assert config["checks"]["enabled"] is False, "only the named checks run"
    assert "not_a_server_error" not in config["checks"]


def test_substitutions_cover_the_operations_that_are_sent(suite: Suite) -> None:
    runs = plan_run(suite, ALL_VALUES, selected={"createThing"})

    assert build_substitutions(suite, ALL_VALUES, runs) == {
        "POST /things": {"body.filters.kb[*]": "K1", "body.models[*].modelKey": "M1"}
    }


def test_created_resources_are_found_in_the_2xx_responses(suite: Suite, tmp_path: Path) -> None:
    events = write_events(
        tmp_path,
        [
            case_event(
                "POST /things", case_id="a", status=201, response={"thing": {"_id": "new-1"}}
            ),
            case_event(
                "POST /things", case_id="b", status=400, response={"thing": {"_id": "not-created"}}
            ),
            case_event("POST /things", case_id="c", status=201, response={"unexpected": True}),
            case_event(
                "GET /things", case_id="d", status=200, response={"thing": {"_id": "listed"}}
            ),
        ],
    )

    assert created_resource_paths(suite, ALL_VALUES, events) == ["/things/new-1?p=P1"]
    assert created_resource_paths(suite, ALL_VALUES, tmp_path / "no-run.ndjson") == []


@pytest.fixture(scope="module")
def planned_cases(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list]:
    """The cases of four enterprise-search operations, generated against the stub."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path_factory.mktemp("contract-reports"))
    try:
        suite = load_suite(ENTERPRISE_SEARCH)
        plan_path = runner.plan(suite, selected=set(PLANNED.values()))
        cases: dict[str, list] = {}
        for case in read_cases(plan_path.parent / "events.ndjson"):
            cases.setdefault(case.label, []).append(case)
        assert "Test cases for each operation" in plan_path.read_text(encoding="utf-8")
        return cases
    finally:
        monkeypatch.undo()


def test_schemathesis_generates_valid_and_invalid_cases(planned_cases: dict[str, list]) -> None:
    assert set(planned_cases) == set(PLANNED)
    for label in ("POST /search", "GET /search", "GET /conversations"):
        modes = {case.is_negative for case in planned_cases[label]}
        assert modes == {True, False}, label


def test_schemathesis_tests_the_boundaries_of_a_range(planned_cases: dict[str, list]) -> None:
    """`GET /search` documents `limit` as 1 to 100."""
    limit = {
        case.description.removeprefix("limit: ")
        for case in planned_cases["GET /search"]
        if case.parameter == "limit"
    }

    assert {
        "Value greater than maximum",
        "Value smaller than minimum",
        "Maximum value",
        "Minimum value",
    } <= limit


def test_a_negative_only_operation_sends_no_valid_request(planned_cases: dict[str, list]) -> None:
    assert all(case.is_negative for case in planned_cases["POST /conversations/create"])


def test_no_case_uses_a_method_the_spec_does_not_list(planned_cases: dict[str, list]) -> None:
    for label, cases in planned_cases.items():
        assert {case.method for case in cases} == {label.split(" ", 1)[0]}


def test_real_values_reach_the_request(planned_cases: dict[str, list]) -> None:
    placeholder = "0" * 24
    with_kb = [case for case in planned_cases["POST /search"] if '"kb": ["' in case.request_body]
    with_project = [
        case for case in planned_cases["GET /conversations"] if "projectId=" in case.target
    ]

    assert with_kb and with_project
    # A valid request always carries the real ID. So does an invalid one whose
    # invalid part is elsewhere: the ID must not be a second reason to reject it.
    assert all(
        f'"kb": ["{placeholder}"' in case.request_body for case in with_kb if not case.is_negative
    )
    assert all(
        f"projectId={placeholder}" in case.target for case in with_project if not case.is_negative
    )
    assert any(
        f"projectId={placeholder}" in case.target for case in with_project if case.is_negative
    )
