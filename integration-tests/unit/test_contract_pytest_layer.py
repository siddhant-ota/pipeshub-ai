"""The pytest side of the contract tests, run for real but against a stub.

A small suite over four real operations of the spec is written into a throwaway
pytest project, with fake clients, and pointed at a local stub API. So the
whole path runs: values from fixtures, the Schemathesis run, one outcome per
operation, the report, the terminal summary and the cleanup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Each test starts pytest, and with it Schemathesis, in a subprocess.
pytestmark = [pytest.mark.unit, pytest.mark.slow]

INTEGRATION_TESTS = Path(__file__).resolve().parents[1]

# The stub answers `{}`. That satisfies the spec for the first, breaks it for the second.
MATCHES = "PATCH /conversations/{conversationId}/archive"
DIFFERS = "GET /search/{searchId}"
SKIPPED = "DELETE /search"
TEST_MODULE = "contract/integration_test_layer_contract.py"

SUITE = r"""
name: layer
include_path_regex: "^/(conversations/\\{conversationId\\}/archive|search|search/\\{searchId\\})$"
path_parameters:
  defaults:
    - path_prefix: /conversations
      values:
        conversationId: conversation.archivable.id
    - path_prefix: /search
      values:
        searchId: search.readonly.id
  operations:
    deleteSearchById:
      searchId: search.disposable.id
skip:
  - operation: deleteSearchHistory
    reason: Deletes all search history of the user.
  - operation: search
    reason: Calls the LLM.
"""

SUITE_CONFTEST = """
from pathlib import Path

import pytest

from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource

SUITE_PATH = Path(__file__).with_name("suite.yaml")
ROLES = ("readonly", "disposable")


@pytest.fixture(scope="module")
def contract_conversation(conversations_client):
    conversation = response_body(conversations_client.create(), (201,), "Create conversation")
    try:
        yield conversation["id"]
    finally:
        delete_quietly("conversation", lambda: conversations_client.delete(conversation["id"]))


@pytest.fixture(scope="module")
def contract_searches(search_client):
    created = {}
    try:
        for role in ROLES:
            created[role] = response_body(search_client.create(role), (200,), "Create search")["id"]
        yield created
    finally:
        for search_id in created.values():
            delete_quietly("search", lambda search_id=search_id: search_client.delete(search_id))


VALUE_SOURCES = (
    ValueSource(
        "contract_conversation",
        ("conversation.archivable.id",),
        lambda conversation_id: (conversation_id,),
        "One conversation, to archive.",
    ),
    ValueSource(
        "contract_searches",
        tuple(f"search.{role}.id" for role in ROLES),
        lambda searches: tuple(searches[role] for role in ROLES),
        "Two saved searches: to read and to delete.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
"""

SUITE_TEST_MODULE = """
from pathlib import Path

import pytest

from helper.contract.outcome import assert_spec_matches_api
from helper.contract.pytest_support import CONTRACT_MARKS, operation_params
from helper.contract.suite import load_suite

SUITE = load_suite(Path(__file__).with_name("suite.yaml"))

pytestmark = CONTRACT_MARKS


@pytest.mark.parametrize("operation_id", operation_params(SUITE))
def test_spec_matches_api(operation_id, contract_run):
    assert_spec_matches_api(contract_run.result_for(operation_id), contract_run.files.report)
"""

ROOT_CONFTEST = """
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w

import helper.contract.config as contract_config
from helper.contract.pytest_support import terminal_summary
from helper.contract.stub_server import stub_server

HERE = Path(__file__).parent
# The stub needs no rate limit, and the test should take seconds.
base = tomllib.loads(contract_config.BASE_CONFIG_PATH.read_text())
base.pop("rate-limit")
contract_config.BASE_CONFIG_PATH = HERE / "schemathesis.base.toml"
contract_config.BASE_CONFIG_PATH.write_bytes(tomli_w.dumps(base).encode())

DELETED = HERE / "deleted.txt"


def pytest_terminal_summary(terminalreporter):
    terminal_summary(terminalreporter)


class Response:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self):
        return self._body


def deleted(what):
    with DELETED.open("a") as log:
        log.write(what + "\\n")
    return Response(200, {})


class Conversations:
    def create(self):
        return Response(201, {"id": "conversation-1"})

    def delete(self, conversation_id):
        return deleted(f"conversation {conversation_id}")


class Searches:
    def create(self, role):
        if SEARCH_FAILS:
            return Response(404, {"error": "No documents are available"})
        return Response(200, {"id": f"search-{role}"})

    def delete(self, search_id):
        return deleted(f"search {search_id}")


@pytest.fixture(scope="session")
def pipeshub_client():
    with stub_server() as url:
        yield SimpleNamespace(base_url=url)


@pytest.fixture(scope="session")
def conversations_client():
    return Conversations()


@pytest.fixture(scope="session")
def search_client():
    return Searches()
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    contract = tmp_path / "contract"
    contract.mkdir()
    for name, content in (
        ("suite.yaml", SUITE),
        ("conftest.py", SUITE_CONFTEST),
        (Path(TEST_MODULE).name, SUITE_TEST_MODULE),
        ("baseline.json", '{"format_version": 1, "findings": []}\n'),
    ):
        (contract / name).write_text(textwrap.dedent(content), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npython_files = integration_test_*.py\nmarkers =\n    contract: contract tests\n",
        encoding="utf-8",
    )
    return tmp_path


def _environment(project: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(INTEGRATION_TESTS),
        "CONTRACT_REPORTS_DIR": str(project / "reports"),
        "CONTRACT_STATIC_AUTHORIZATION": "Bearer stub",
    }


def _run(
    project: Path, *selected: str, search_fails: bool = False
) -> subprocess.CompletedProcess[str]:
    (project / "conftest.py").write_text(
        f"SEARCH_FAILS = {search_fails}\n{textwrap.dedent(ROOT_CONFTEST)}", encoding="utf-8"
    )
    # Node IDs, not `-k`: an operation label has characters that `-k` does not accept.
    node_ids = [f"{TEST_MODULE}::test_spec_matches_api[{label}]" for label in selected]
    return subprocess.run(
        [sys.executable, "-m", "pytest", *node_ids, "-rA", "-p", "no:cacheprovider"],
        cwd=project,
        env=_environment(project),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_each_operation_gets_the_outcome_of_its_verdict(project: Path) -> None:
    result = _run(project, MATCHES, DIFFERS, SKIPPED)
    output = result.stdout

    assert f"PASSED {TEST_MODULE}::test_spec_matches_api[{MATCHES}]" in output, output
    assert f"FAILED {TEST_MODULE}::test_spec_matches_api[{DIFFERS}]" in output
    assert f"SKIPPED [1] {TEST_MODULE}" in output
    assert "Deletes all search history of the user" in output
    assert "1 failed, 1 passed, 1 skipped" in output

    # The failure says what differs and where the report is, without a traceback.
    assert f"{DIFFERS}: the spec does not describe what the API does." in output
    assert "response_schema_conformance" in output
    assert "Traceback" not in output

    # The terminal summary names the gap that the suite declares.
    assert "API contract: layer" in output
    assert "Contract: DIFFERS — " in output
    assert f"Skipped: {SKIPPED} — Deletes all search history" in output

    # Only the selected operations were sent, with the values from the fixtures.
    manifest = (project / "reports/layer/run/manifest.json").read_text(encoding="utf-8")
    assert manifest.count('"state": "full"') == 2
    values = json.loads(
        (project / "reports/layer/run/substitutions.json").read_text(encoding="utf-8")
    )
    assert values == {
        MATCHES: {"path.conversationId": "conversation-1"},
        DIFFERS: {"path.searchId": "search-readonly"},
    }

    # The report says which fixtures gave the values, and with which value.
    report = (project / "reports/layer/run/report.md").read_text(encoding="utf-8")
    assert "## Fixtures" in report
    assert "| `contract_conversation` | added | One conversation, to archive. |" in report
    assert "`search.readonly.id` = `search-readonly`" in report

    # The fixtures are torn down when the tests of the suite are done.
    assert (project / "deleted.txt").read_text(encoding="utf-8").splitlines() == [
        "search search-readonly",
        "search search-disposable",
        "conversation conversation-1",
    ]


def test_a_known_difference_is_an_expected_failure(project: Path) -> None:
    first = _run(project, DIFFERS)
    assert "1 failed" in first.stdout, first.stdout

    accept = subprocess.run(
        [sys.executable, "-m", "helper.contract", "accept", str(project / "contract/suite.yaml")],
        env=_environment(project),
        capture_output=True,
        text=True,
        check=False,
    )
    assert accept.returncode == 0, accept.stderr
    assert "added" in accept.stdout

    second = _run(project, DIFFERS)
    assert "1 xfailed" in second.stdout, second.stdout
    assert "Contract: MATCHES (with known differences)" in second.stdout


def test_a_fixture_that_fails_fails_only_the_operations_that_need_it(project: Path) -> None:
    result = _run(project, MATCHES, DIFFERS, search_fails=True)
    output = result.stdout

    assert f"PASSED {TEST_MODULE}::test_spec_matches_api[{MATCHES}]" in output, output
    assert f"FAILED {TEST_MODULE}::test_spec_matches_api[{DIFFERS}]" in output
    assert (
        "Missing value: search.readonly.id (fixture `contract_searches` failed: AssertionError"
        in output
    )
    assert "No documents are available" in output
