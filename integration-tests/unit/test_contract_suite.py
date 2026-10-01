"""A contract suite must agree with the spec before any request is sent.

The loader is what keeps a spec change from going untested: a new operation,
path parameter or ID field in scope has to be decided on in the suite file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from contract_samples import SPEC, SUITE, write_suite

from helper.contract.fields import field_name, pointer_path, request_fields
from helper.contract.runner import SUITES_ROOT, suite_paths
from helper.contract.spec import operations_in_scope
from helper.contract.suite import (
    AUTH_NONE,
    AUTH_OAUTH_CLIENT,
    AUTH_SESSION,
    AUTH_TOKEN,
    PROFILE_EXAMPLES_ONLY,
    PROFILE_FULL,
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    SuiteError,
    load_suite,
)

pytestmark = pytest.mark.unit

SUITES = suite_paths()


def _suite_id(suite_path: Path) -> str:
    return str(suite_path.parent.relative_to(SUITES_ROOT))


@pytest.mark.parametrize("suite_path", SUITES, ids=_suite_id)
def test_every_suite_in_the_repository_agrees_with_the_spec(suite_path: Path) -> None:
    suite = load_suite(suite_path)

    assert suite.operations
    assert suite.name not in {load_suite(other).name for other in SUITES if other != suite_path}


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
            negative_only=[{"reason": "Costs money.", "operations": ["createThing"]}],
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
        write_suite(tmp_path, examples_only=[{"reason": "Slow.", "operations": ["listThings"]}]),
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
                "negative_only": [{"reason": "x", "operations": ["listThings"]}],
                "examples_only": [{"reason": "x", "operations": ["listThings"]}],
            },
            "listThings is in `examples_only` and in another list",
        ),
        (
            {"values": {**SUITE["values"], "path.thingId": "thing.id"}},
            "path parameters get their value under `path_parameters`: path.thingId",
        ),
        (
            {"values_by_operation": {"listThings": {"body.name": "thing.name"}}},
            "`values_by_operation` names field(s) it does not have: body.name",
        ),
        (
            {"auth": {"listThings": "cookie"}},
            "`auth` must be one of oauth_client, session, none or `{token: <value key>}`",
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


def test_a_value_for_one_operation_wins_over_the_value_for_the_suite(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            values_by_operation={
                "listThings": {"query.projectId": "project.other.id"},
                "createThing": {"body.name": "thing.name", "body.ownerId": "user.id"},
            },
        ),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    assert by_id["listThings"].field_values == {"query.projectId": "project.other.id"}
    # A field that holds no ID can get a value too, and so can one the suite lists otherwise.
    assert by_id["createThing"].field_values["body.name"] == "thing.name"
    assert by_id["createThing"].field_values["body.ownerId"] == "user.id"


def test_an_operation_can_need_a_value_that_goes_into_no_request(tmp_path: Path) -> None:
    """For example a fixture that saves a setting first and puts it back at the end."""
    suite = load_suite(
        write_suite(tmp_path, requires={"createThing": ["settings.saved"]}),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    assert "settings.saved" in by_id["createThing"].value_keys
    assert "settings.saved" not in by_id["createThing"].field_values.values()
    assert "settings.saved" in suite.fixture_keys


def test_a_constant_is_a_value_that_needs_no_fixture(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            constants={"kb.id": "KB-1"},
        ),
        SPEC,
    )

    assert suite.constants == {"kb.id": "KB-1"}
    assert "kb.id" in suite.value_keys
    assert "kb.id" not in suite.fixture_keys


def _secured(security: list[dict[str, list]] | None, **operation: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {"operationId": "getThing", "responses": {"200": {}}, **operation}
    if security is not None:
        definition["security"] = security
    return {
        "servers": [{"url": "{instance_url}/api/v1"}],
        "security": [{"bearerAuth": []}, {"oauth2": []}],
        "paths": {"/thing": {"get": definition}},
    }


@pytest.mark.parametrize(
    ("security", "login"),
    [
        # The default of the spec: a session token or an OAuth token.
        (None, AUTH_OAUTH_CLIENT),
        ([{"bearerAuth": []}], AUTH_SESSION),
        ([], AUTH_NONE),
    ],
)
def test_the_spec_decides_how_an_operation_logs_in(
    tmp_path: Path, security: list | None, login: str
) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            include_path_regex="^/thing$",
            path_parameters={},
            values={},
            client_chosen_ids=[],
            ids_without_fixture=[],
        ),
        _secured(security),
    )

    assert [planned.auth for planned in suite.operations] == [login]
    assert suite.logins == {login}
    assert suite.api_prefix == "/api/v1"


def test_an_operation_that_takes_a_special_token_needs_a_decision(tmp_path: Path) -> None:
    empty = {
        "include_path_regex": "^/thing$",
        "path_parameters": {},
        "values": {},
        "client_chosen_ids": [],
        "ids_without_fixture": [],
    }
    spec = _secured([{"scopedToken": []}])

    with pytest.raises(SuiteError, match="the spec accepts only scopedToken"):
        load_suite(write_suite(tmp_path, **empty), spec)

    suite = load_suite(
        write_suite(tmp_path, **empty, auth={"getThing": {"token": "thing.token"}}), spec
    )
    (planned,) = suite.operations
    assert (planned.auth, planned.token_key) == (AUTH_TOKEN, "thing.token")
    assert planned.value_keys == {"thing.token"}


def test_a_root_level_operation_has_no_api_prefix(tmp_path: Path) -> None:
    empty = {
        "include_path_regex": "^/thing",
        "path_parameters": {},
        "values": {},
        "client_chosen_ids": [],
        "ids_without_fixture": [],
    }
    spec = _secured(None, servers=[{"url": "/"}])

    assert load_suite(write_suite(tmp_path, **empty), spec).api_prefix == ""

    spec["paths"]["/things"] = {"get": {"operationId": "listThings", "responses": {"200": {}}}}
    with pytest.raises(SuiteError, match="do not have one base path"):
        load_suite(write_suite(tmp_path, **empty), spec)


def test_a_path_parameter_with_listed_values_needs_no_value(tmp_path: Path) -> None:
    spec = {
        "paths": {
            "/things/{kind}/{thingId}": {
                "get": {
                    "operationId": "getThing",
                    "parameters": [
                        {
                            "name": "kind",
                            "in": "path",
                            "schema": {"type": "string", "enum": ["a", "b"]},
                        },
                        {"name": "thingId", "in": "path", "schema": {"type": "string"}},
                    ],
                }
            }
        }
    }
    keys = {
        "include_path_regex": "^/things",
        "values": {},
        "client_chosen_ids": [],
        "ids_without_fixture": [],
    }

    (planned,) = load_suite(write_suite(tmp_path, **keys), spec).operations
    assert planned.path_values == {"thingId": "thing.id"}

    with pytest.raises(SuiteError, match=r"no value for path parameter\(s\) thingId$"):
        load_suite(write_suite(tmp_path, **keys, path_parameters={}), spec)


def test_a_field_under_a_free_form_key_can_get_a_value(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            ids_without_fixture=[],
            values={**SUITE["values"], "body.notes{*}.authorId": "user.id"},
        ),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    assert by_id["createThing"].field_values["body.notes{*}.authorId"] == "user.id"


def test_a_constant_keeps_its_type(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(tmp_path, constants={"thing.enabled": False, "thing.size": 3}), SPEC
    )

    assert suite.constants == {"thing.enabled": False, "thing.size": 3}


def _upload_spec(file_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "paths": {
            "/files/{fileId}": {
                "put": {
                    "operationId": "replaceFile",
                    "parameters": [
                        {
                            "name": "fileId",
                            "in": "path",
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "file": file_schema,
                                        "folderId": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                }
            }
        }
    }


@pytest.mark.parametrize(
    ("file_schema", "name"),
    [
        ({"type": "string", "format": "binary"}, "body.file"),
        ({"type": "array", "items": {"type": "string", "format": "binary"}}, "body.file[*]"),
    ],
)
def test_a_file_of_a_multipart_body_gets_a_file(
    tmp_path: Path, file_schema: dict[str, Any], name: str
) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            include_path_regex="^/files",
            path_parameters={
                "defaults": [{"path_prefix": "/files", "values": {"fileId": "file.id"}}]
            },
            values={"body.folderId": "folder.id", name: "file.path"},
            client_chosen_ids=[],
            ids_without_fixture=[],
        ),
        _upload_spec(file_schema),
    )
    (planned,) = suite.operations

    assert planned.file_fields == (name,)
    assert planned.field_values == {"body.folderId": "folder.id", name: "file.path"}
    # The path parameter is a UUID in the spec, so a plan must put a UUID there.
    assert suite.uuid_keys == {"file.id"}


def test_a_header_for_an_operation(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            headers={"listThings": {"Accept": "stream.accept"}},
            constants={"stream.accept": "text/event-stream"},
        ),
        SPEC,
    )
    by_id = {planned.operation.operation_id: planned for planned in suite.operations}

    # The value of a header is a value key, like every other value of the suite.
    assert by_id["listThings"].header_values == {"Accept": "stream.accept"}
    assert "stream.accept" in by_id["listThings"].value_keys
    assert by_id["createThing"].header_values == {}
