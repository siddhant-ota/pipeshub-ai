"""What a run does with each operation, and the Schemathesis side of it.

The last tests start Schemathesis against a local stub. They pin how the
installed Schemathesis version generates cases and writes its NDJSON report,
which is everything the contract tests read from it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from contract_samples import SPEC, case_event, write_events, write_suite

from helper.contract import runner
from helper.contract.config import (
    STATE_DESELECTED,
    STATE_EXAMPLES_ONLY,
    STATE_FULL,
    STATE_NEGATIVE_ONLY,
    STATE_SKIPPED,
    STATE_VALUE_MISSING,
    build_config,
    build_logins,
    build_substitutions,
    build_tokens,
    plan_run,
)
from helper.contract.created import Leftover, created_resources
from helper.contract.events import read_cases
from helper.contract.stub_server import stub_server
from helper.contract.suite import AUTH_NONE, AUTH_SESSION, Suite, load_suite
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
    "PUT /conversations/{conversationId}/project": "setConversationProject",
}
ALL_VALUES = ContractValues(
    values={"thing.id": "T1", "project.id": "P1", "kb.id": "K1", "llm.key": "M1"}
)


@pytest.fixture
def suite(tmp_path: Path) -> Suite:
    return load_suite(
        write_suite(
            tmp_path,
            negative_only=[{"reason": "Costs money.", "operations": ["createThing"]}],
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


def test_config_switches_operations_off_and_limits_what_is_generated(suite: Suite) -> None:
    values = ContractValues(values={"thing.id": "T1", "kb.id": "K1", "llm.key": "M1"})
    runs = plan_run(suite, values)

    config = build_config(suite, runs)
    blocks = {block["include-operation-id"]: block for block in config["operations"]}

    # An operation that is sent in full needs no block: the hooks give it its values.
    assert "deleteThing" not in blocks
    assert blocks["listThings"] == {"include-operation-id": "listThings", "enabled": False}
    assert blocks["createThing"]["generation"] == {"mode": "negative"}
    assert blocks["createThing"]["phases"] == {"examples": {"enabled": False}}
    assert config["hooks"].endswith("helper/contract/hooks.py")
    assert config["checks"]["enabled"] is False, "only the named checks run"
    assert "not_a_server_error" not in config["checks"]


def test_config_of_an_examples_only_operation(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(tmp_path, examples_only=[{"reason": "Slow.", "operations": ["listThings"]}]),
        SPEC,
    )
    runs = plan_run(suite, ALL_VALUES)
    by_id = {run.operation_id: run for run in runs}

    assert by_id["listThings"].state == STATE_EXAMPLES_ONLY
    assert by_id["listThings"].is_sent
    blocks = {
        block["include-operation-id"]: block for block in build_config(suite, runs)["operations"]
    }
    # Invalid requests from the coverage phase; the examples phase is left on, unlike negative-only.
    assert blocks["listThings"] == {
        "include-operation-id": "listThings",
        "generation": {"mode": "negative"},
    }


@pytest.mark.parametrize(
    ("stop_reason", "raises"),
    [("completed", False), ("server_unavailable", True), ("interrupted", True), (None, True)],
)
def test_a_run_that_stopped_early_is_not_judged(
    suite: Suite,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str | None,
    raises: bool,
) -> None:
    """Schemathesis reports `complete: true` even when it gave up, so the stop reason decides."""
    files = runner.RunFiles(tmp_path)

    def schemathesis_stub(command: list[str], **kwargs: object) -> SimpleNamespace:
        files.summary.write_text(json.dumps({"complete": True, "stop_reason": stop_reason}))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner, "_schemathesis_command", lambda: "schemathesis")
    monkeypatch.setattr(runner.subprocess, "run", schemathesis_stub)

    if raises:
        with pytest.raises(runner.RunnerError, match="stopped early"):
            runner._run_schemathesis(suite, files, api_url="http://stub", env={})
    else:
        runner._run_schemathesis(suite, files, api_url="http://stub", env={})


def test_a_new_run_discards_the_results_of_the_one_before(tmp_path: Path) -> None:
    """Otherwise a run that fails to start would be judged, and cleaned up, with old results."""
    files = runner.RunFiles(tmp_path)
    for path in (files.manifest, files.events, files.summary, files.har):
        path.write_text("old")

    runner._discard_previous_run(files)

    assert not any(
        path.exists() for path in (files.manifest, files.events, files.summary, files.har)
    )


def test_substitutions_cover_the_operations_that_are_sent(suite: Suite) -> None:
    runs = plan_run(suite, ALL_VALUES, selected={"createThing", "deleteThing"})

    assert build_substitutions(suite, ALL_VALUES, runs) == {
        "POST /things": {"body.filters.kb[*]": "K1", "body.models[*].modelKey": "M1"},
        "DELETE /things/{thingId}": {"path.thingId": "T1"},
    }


def test_rate_limits_of_the_suite_and_of_an_operation(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(tmp_path, rate_limit="100/m", operation_rate_limits={"listThings": "5/m"}),
        SPEC,
    )

    config = build_config(suite, plan_run(suite, ALL_VALUES))

    assert config["rate-limit"] == "100/m"
    assert {"include-operation-id": "listThings", "rate-limit": "5/m"} in config["operations"]


def test_each_operation_logs_in_the_way_the_suite_says(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path, auth={"listThings": AUTH_SESSION, "deleteThing": {"token": "thing.token"}}
        ),
        SPEC,
    )
    values = ContractValues(values={**ALL_VALUES.values, "thing.token": "secret-token"})
    runs = plan_run(suite, values)

    assert build_logins(suite, runs) == {
        "GET /things": AUTH_SESSION,
        "POST /things": AUTH_NONE,
        "DELETE /things/{thingId}": "token",
    }
    assert build_tokens(suite, values, runs) == {"DELETE /things/{thingId}": "secret-token"}
    # The token is a value like any other: without it the operation is not sent.
    without = {run.operation_id: run for run in plan_run(suite, ALL_VALUES)}
    assert without["deleteThing"].state == STATE_VALUE_MISSING
    assert "thing.token" in without["deleteThing"].reason


def test_an_operation_whose_login_is_not_available_is_not_sent(tmp_path: Path) -> None:
    suite = load_suite(write_suite(tmp_path, auth={"listThings": AUTH_SESSION}), SPEC)
    values = ContractValues(
        values=dict(ALL_VALUES.values), no_login={AUTH_SESSION: "no test user is configured"}
    )

    runs = {run.operation_id: run for run in plan_run(suite, values)}

    assert runs["listThings"].state == STATE_VALUE_MISSING
    assert runs["listThings"].reason == "No `session` login: no test user is configured"
    assert runs["deleteThing"].state == STATE_FULL


def test_a_credential_is_not_written_to_the_files_of_a_run(suite: Suite, tmp_path: Path) -> None:
    values = ContractValues(values=dict(ALL_VALUES.values), secret={"kb.id"})

    _, _, env = runner._prepare(suite, values, runner.RunFiles(tmp_path), None)

    assert '"K1"' not in runner.RunFiles(tmp_path).substitutions.read_text(encoding="utf-8")
    assert "(secret)" in runner.RunFiles(tmp_path).substitutions.read_text(encoding="utf-8")
    assert '"K1"' in env["CONTRACT_SUBSTITUTIONS"], "the hooks still get the real value"


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

    # It is deleted with the login of the operation that created it.
    assert created_resources(suite, ALL_VALUES, events) == [
        Leftover("/things/new-1?p=P1", AUTH_NONE)
    ]
    assert created_resources(suite, ALL_VALUES, tmp_path / "no-run.ndjson") == []


def test_a_created_resource_can_be_an_item_of_a_list(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            created_resources=[
                {
                    "operation": "createThing",
                    "id_pointer": "/things/0/_id",
                    "delete_path": "/things/{id}",
                }
            ],
        ),
        SPEC,
    )
    events = write_events(
        tmp_path,
        [case_event("POST /things", case_id="a", status=201, response={"things": [{"_id": "n"}]})],
    )

    assert [leftover.path for leftover in created_resources(suite, ALL_VALUES, events)] == [
        "/things/n"
    ]


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


def test_an_examples_only_operation_sends_the_spec_examples_as_its_valid_requests(
    planned_cases: dict[str, list],
) -> None:
    """`POST /search` is `examples_only` in the suite: a valid search calls the LLM twice."""
    valid = [case for case in planned_cases["POST /search"] if not case.is_negative]
    invalid = [case for case in planned_cases["POST /search"] if case.is_negative]

    assert valid and {case.phase for case in valid} == {"examples"}
    assert len(invalid) > len(valid)


def test_no_case_uses_a_method_the_spec_does_not_list(planned_cases: dict[str, list]) -> None:
    for label, cases in planned_cases.items():
        assert {case.method for case in cases} == {label.split(" ", 1)[0]}


def test_real_values_reach_the_request(planned_cases: dict[str, list]) -> None:
    placeholder = "0" * 24
    in_body = [
        case
        for case in planned_cases["PUT /conversations/{conversationId}/project"]
        if '"projectId": "' in case.request_body
    ]
    with_project = [
        case for case in planned_cases["GET /conversations"] if "projectId=" in case.target
    ]

    assert in_body and with_project
    # A valid request always carries the real ID. So does an invalid one whose
    # invalid part is elsewhere: the ID must not be a second reason to reject it.
    assert any(not case.is_negative for case in in_body)
    assert all(
        f'"projectId": "{placeholder}"' in case.request_body
        for case in in_body
        if not case.is_negative
    )
    assert all(
        f"projectId={placeholder}" in case.target for case in with_project if not case.is_negative
    )
    assert any(
        f"projectId={placeholder}" in case.target for case in with_project if case.is_negative
    )


def _path_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **suite_keys: object) -> list:
    """The cases of one real operation whose path parameter is an enum, against the stub."""
    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path / "reports")
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        yaml.safe_dump(
            {
                "name": "path-values",
                "include_path_regex": "^/configurationManager/ai-models/available/",
                **suite_keys,
            }
        ),
        encoding="utf-8",
    )
    plan_path = runner.plan(load_suite(suite_path))
    return list(read_cases(plan_path.parent / "events.ndjson"))


def _last_segment(target: str) -> str:
    return target.split("?")[0].rsplit("/", 1)[1]


def test_a_path_parameter_with_listed_values_needs_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec lists the values of `modelType`, and Schemathesis sends each of them."""
    cases = _path_cases(tmp_path, monkeypatch)

    valid = {_last_segment(case.target) for case in cases if not case.is_negative}
    assert {"llm", "embedding"} <= valid
    assert any(case.is_negative for case in cases)


def test_a_path_value_leaves_the_invalid_path_of_a_negative_case_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = _path_cases(
        tmp_path,
        monkeypatch,
        path_parameters={"defaults": [{"path_prefix": "/", "values": {"modelType": "model.type"}}]},
        # A value the spec allows. One it forbids would turn every case into an invalid one:
        # Schemathesis looks at the request again after the value is in.
        constants={"model.type": "llm"},
    )

    valid = {_last_segment(case.target) for case in cases if not case.is_negative}
    about_the_path = {
        _last_segment(case.target) for case in cases if case.is_negative and case.location == "path"
    }

    assert valid == {"llm"}
    assert about_the_path and "llm" not in about_the_path


def test_a_redirect_is_recorded_and_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 302 is what the spec documents. Its target can be any host a request names."""
    followed = tmp_path / "followed"

    @contextmanager
    def redirecting_stub() -> Iterator[str]:
        with stub_server() as target, stub_server(redirect_to=f"{target}/followed") as url:
            yield url
            # The second stub answers 200 to anything, so a followed redirect would show as 200.
            followed.write_text(target)

    monkeypatch.setattr(runner, "stub_server", redirecting_stub)

    cases = _path_cases(tmp_path, monkeypatch)

    assert followed.exists()
    assert {case.status for case in cases} == {302}


def _planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **suite_keys: object) -> Path:
    """Plan a suite over real operations against the stub; returns the folder with its files."""
    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path / "reports")
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump({"name": "planned", **suite_keys}), encoding="utf-8")
    return runner.plan(load_suite(suite_path)).parent


def test_an_upload_sends_the_real_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schemathesis generates an empty file named after its form field; an API rejects that."""
    picture = tmp_path / "contract-picture.png"
    picture.write_bytes(b"\x89PNG real picture bytes")

    folder = _planned(
        tmp_path,
        monkeypatch,
        include_path_regex="^/users/dp$",
        values_by_operation={"uploadUserDisplayPicture": {"body.file": "picture.path"}},
        constants={"picture.path": str(picture)},
    )
    uploads = [
        case for case in read_cases(folder / "events.ndjson") if case.label == "PUT /users/dp"
    ]

    with_file = [case for case in uploads if 'name="file"' in case.request_body]
    assert any(not case.is_negative for case in with_file)
    for case in with_file:
        assert 'filename="contract-picture.png"' in case.request_body
        assert "real picture bytes" in case.request_body
        assert "Content-Type: image/png" in case.request_body
    # The case that leaves the file out still leaves it out.
    assert any(case.is_negative and case not in with_file for case in uploads)


TITLE = "PATCH /conversations/{conversationId}/title"


def test_a_name_that_is_different_in_each_request_and_a_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _planned(
        tmp_path,
        monkeypatch,
        include_path_regex=r"^/conversations/\{conversationId\}/title$",
        path_parameters={
            "defaults": [{"path_prefix": "/", "values": {"conversationId": "conversation.id"}}]
        },
        values_by_operation={"updateConversationTitle": {"body.title": "title.unique"}},
        constants={"title.unique": "contract-{case}", "header.value": "yes"},
        headers={"updateConversationTitle": {"X-Contract-Test": "header.value"}},
    )
    titles = [
        case.request_json()["title"]
        for case in read_cases(folder / "events.ndjson")
        if isinstance(case.request_json(), dict)
        and isinstance(case.request_json().get("title"), str)
        and case.request_json()["title"].startswith("contract-")
    ]

    assert len(titles) > 1
    assert len(set(titles)) == len(titles)
    assert all(title.removeprefix("contract-").isdigit() for title in titles)
    har = json.loads((folder / "requests.har").read_text(encoding="utf-8"))
    for entry in har["log"]["entries"]:
        headers = {header["name"]: header["value"] for header in entry["request"]["headers"]}
        assert headers.get("X-Contract-Test") == "yes"


def test_a_value_goes_into_a_request_that_is_invalid_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown property, or a missing one, next to an ID: the ID must still be the real one.

    Otherwise the API can reject the request for the generated ID, and the test would pass
    without showing that the API saw the invalid part.
    """
    folder = _planned(
        tmp_path,
        monkeypatch,
        include_path_regex=r"^/conversations/\{conversationId\}/project$",
        path_parameters={
            "defaults": [{"path_prefix": "/", "values": {"conversationId": "conversation.id"}}]
        },
        values={"body.projectId": "project.id"},
        constants={"project.id": "a" * 24},
    )
    with_project = [
        case
        for case in read_cases(folder / "events.ndjson")
        if isinstance(case.request_json(), dict) and "projectId" in case.request_json()
    ]

    unknown_property = [case for case in with_project if "unexpected properties" in case.what]
    assert unknown_property
    assert all(case.request_json()["projectId"] == "a" * 24 for case in unknown_property)
    # The cases about the field itself keep what they generated.
    about_the_field = [
        case for case in with_project if case.is_negative and "projectId" in case.schema_pointer
    ]
    assert about_the_field
    assert all(case.request_json()["projectId"] != "a" * 24 for case in about_the_field)


def test_a_run_refuses_a_file_that_is_not_there(suite: Suite, tmp_path: Path) -> None:
    upload = load_suite(
        write_suite(
            tmp_path,
            include_path_regex="^/files",
            path_parameters={"defaults": [{"path_prefix": "/files", "values": {"fileId": "f"}}]},
            values={"body.file": "file.path"},
            client_chosen_ids=[],
            ids_without_fixture=[],
            constants={"f": "F1", "file.path": str(tmp_path / "absent.txt")},
        ),
        {
            "paths": {
                "/files/{fileId}": {
                    "put": {
                        "operationId": "replaceFile",
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "file": {"type": "string", "format": "binary"}
                                        },
                                    }
                                }
                            }
                        },
                    }
                }
            }
        },
    )
    values = ContractValues(values=dict(upload.constants))

    with pytest.raises(runner.RunnerError, match="a file that does not exist: .*absent.txt"):
        runner.execute(upload, values, base_url="http://stub")


def test_one_operation_can_create_several_things(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            auth={"createThing": {"token": "thing.token"}},
            created_resources=[
                {
                    "operation": "createThing",
                    "id_pointer": f"/things/{index}/_id",
                    "delete_path": "/things/{id}",
                }
                for index in (0, 1)
            ],
        ),
        SPEC,
    )
    events = write_events(
        tmp_path,
        [
            case_event(
                "POST /things",
                case_id="a",
                status=201,
                response={"things": [{"_id": "n1"}, {"_id": "n2"}]},
            )
        ],
    )

    # Each is deleted with the token that the operation logs in with.
    assert created_resources(suite, ALL_VALUES, events) == [
        Leftover("/things/n1", "token", "thing.token"),
        Leftover("/things/n2", "token", "thing.token"),
    ]


def test_an_operation_can_be_sent_after_all_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For one that removes what the others need. Schemathesis itself keeps the order of the spec."""
    suite_keys = {
        "include_path_regex": r"^/conversations/\{conversationId\}/(title|archive|unarchive)$",
        "path_parameters": {
            "defaults": [{"path_prefix": "/", "values": {"conversationId": "conversation.id"}}]
        },
    }

    def _order(**more: object) -> list[str]:
        folder = _planned(tmp_path, monkeypatch, **suite_keys, **more)
        return [case.label for case in read_cases(folder / "events.ndjson")]

    in_spec_order = _order()
    first = in_spec_order[0]
    first_id = {
        "PATCH /conversations/{conversationId}/title": "updateConversationTitle",
        "PATCH /conversations/{conversationId}/archive": "archiveConversation",
        "PATCH /conversations/{conversationId}/unarchive": "unarchiveConversation",
    }[first]

    moved = _order(last=[first_id])

    assert sorted(moved) == sorted(in_spec_order), "the same cases are sent"
    count = in_spec_order.count(first)
    assert set(moved[-count:]) == {first}
    assert first not in moved[:-count]
