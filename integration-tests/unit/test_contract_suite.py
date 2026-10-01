"""A contract suite must agree with the spec before any request is sent.

The loader is what keeps a spec change from going untested: a new operation,
path parameter or ID field in scope has to be decided on in the suite file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from contract_samples import SPEC, SUITE, write_suite

from helper.contract.fields import field_name, pointer_path, request_fields
from helper.contract.spec import operations_in_scope
from helper.contract.suite import (
    PROFILE_EXAMPLES_ONLY,
    PROFILE_FULL,
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    SuiteError,
    load_suite,
)

pytestmark = pytest.mark.unit

SUITES = sorted(
    (Path(__file__).resolve().parents[1] / "response-validation").glob("**/contract/suite.yaml")
)


@pytest.mark.parametrize("suite_path", SUITES, ids=lambda path: path.parent.parent.name)
def test_every_suite_in_the_repository_agrees_with_the_spec(suite_path: Path) -> None:
    suite = load_suite(suite_path)

    assert suite.operations
    for planned in suite.operations:
        if planned.profile != PROFILE_SKIP:
            assert set(planned.path_values) == set(planned.operation.path_parameters)


def test_operations_in_scope_follow_the_path_filter() -> None:
    operations = operations_in_scope(SPEC, "^/things")

    assert [operation.operation_id for operation in operations] == [
        "listThings",
        "createThing",
        "deleteThing",
    ]
    by_id = {operation.operation_id: operation for operation in operations}
    assert by_id["listThings"].sdk and not by_id["createThing"].sdk
    assert by_id["deleteThing"].path_parameters == ("thingId",)
    assert by_id["deleteThing"].label == "DELETE /things/{thingId}"


def test_request_fields_mark_the_fields_that_hold_an_id() -> None:
    by_id = {
        operation.operation_id: operation for operation in operations_in_scope(SPEC, "^/things")
    }

    listed = {field.name: field.is_id for field in request_fields(SPEC, by_id["listThings"])}
    created = {field.name: field.is_id for field in request_fields(SPEC, by_id["createThing"])}

    assert listed == {"query.limit": False, "query.projectId": True}
    assert created == {
        "body.name": False,
        "body.ownerId": True,
        # Named `kb`; only the description of the array says it holds IDs.
        "body.filters.kb[*]": True,
        # An enum is a closed list of words, never an ID.
        "body.filters.sort": False,
        "body.models[*].modelKey": True,
        "body.models[*].provider": False,
        # Named exactly `id`.
        "body.tags[*].id": True,
        "body.tags[*].label": False,
        # Under a key the spec does not name.
        "body.notes{*}.authorId": True,
        # `parent` is the schema itself; it is followed once and not again.
    }


@pytest.mark.parametrize(
    ("name", "is_id"),
    [
        ("id", True),
        ("ids", True),
        ("key", True),
        ("projectId", True),
        ("userIds", True),
        ("record_id", True),
        ("modelKey", True),
        ("orgID", True),
        # Words that only end in the same letters.
        ("paid", False),
        ("valid", False),
        ("monkey", False),
        ("kb", False),
    ],
)
def test_which_field_names_hold_an_id(name: str, is_id: bool) -> None:
    spec = {
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "parameters": [{"name": name, "in": "query", "schema": {"type": "string"}}],
                }
            }
        }
    }
    (operation,) = operations_in_scope(spec, "^/things")

    assert [field.is_id for field in request_fields(spec, operation)] == [is_id]


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("", "body"),
        ("/type", "body"),
        ("/properties/name/type", "body.name"),
        ("/allOf/0/properties/filters/properties/kb/items/type", "body.filters.kb[*]"),
        ("/properties/models/items/properties/modelKey", "body.models[*].modelKey"),
    ],
)
def test_a_schema_pointer_names_the_field_it_points_into(pointer: str, expected: str) -> None:
    assert field_name("body", pointer_path(pointer)) == expected


def test_suite_gives_each_operation_its_values(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            negative_only={"reason": "Costs money.", "operations": ["createThing"]},
            skip=[{"operation": "deleteThing", "reason": "Destructive."}],
        ),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    assert by_id["listThings"].profile == PROFILE_FULL
    assert by_id["listThings"].field_values == {"query.projectId": "project.id"}
    assert by_id["createThing"].profile == PROFILE_NEGATIVE_ONLY
    assert by_id["createThing"].reason == "Costs money."
    assert by_id["createThing"].field_values == {
        "body.filters.kb[*]": "kb.id",
        "body.models[*].modelKey": "llm.key",
    }
    assert by_id["deleteThing"].profile == PROFILE_SKIP
    # A skipped operation needs none of its values.
    assert suite.value_keys == {"project.id", "kb.id", "llm.key"}
    # Only a field that must name something real excuses a rejected request.
    assert by_id["createThing"].fixtureless_fields == ("body.notes{*}.authorId",)


def test_an_examples_only_operation(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(tmp_path, examples_only={"reason": "Slow.", "operations": ["listThings"]}),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    assert by_id["listThings"].profile == PROFILE_EXAMPLES_ONLY
    assert by_id["listThings"].reason == "Slow."


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"path_parameters": {}}, "no value for path parameter(s) thingId"),
        (
            {"client_chosen_ids": []},
            "`client_chosen_ids` or `ids_without_fixture`: body.ownerId, body.tags[*].id",
        ),
        (
            {"skip": [{"operation": "renamedThing", "reason": "x"}]},
            "operations that are not in scope of the spec: renamedThing",
        ),
        (
            {"values": {**SUITE["values"], "body.goneId": "x.id"}},
            "fields that no operation has: body.goneId",
        ),
        (
            {"values": {**SUITE["values"], "body.ownerId": "user.id"}},
            "fields in more than one list: body.ownerId",
        ),
        (
            {
                "negative_only": {"operations": ["listThings"]},
                "examples_only": {"operations": ["listThings"]},
            },
            "listThings is in `examples_only` and in another list",
        ),
        (
            {
                "ids_without_fixture": [],
                "values": {**SUITE["values"], "body.notes{*}.authorId": "user.id"},
            },
            "`values` cannot set a field under a free-form key",
        ),
    ],
)
def test_suite_that_disagrees_with_the_spec_is_rejected(
    tmp_path: Path, changes: dict, message: str
) -> None:
    with pytest.raises(SuiteError) as error:
        load_suite(write_suite(tmp_path, **changes), SPEC)

    assert message in str(error.value)


def test_suite_reports_every_problem_at_once(tmp_path: Path) -> None:
    with pytest.raises(SuiteError) as error:
        load_suite(write_suite(tmp_path, path_parameters={}, client_chosen_ids=[]), SPEC)

    assert "thingId" in str(error.value)
    assert "body.ownerId" in str(error.value)
