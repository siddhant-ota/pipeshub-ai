"""The pytest side of the contract tests, run for real but against a stub.

The enterprise-search contract `conftest.py` and test module are copied into a
throwaway pytest project. Its root conftest replaces the integration-test
fixtures with fakes and points the run at a local stub API, so the whole path
runs: values from fixtures, the Schemathesis run, one outcome per operation,
the terminal summary and the cleanup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Each test starts pytest, and with it Schemathesis, in a subprocess.
pytestmark = [pytest.mark.unit, pytest.mark.slow]

INTEGRATION_TESTS = Path(__file__).resolve().parents[1]
CONTRACT_DIR = INTEGRATION_TESTS / "response-validation/enterprise-search/contract"

# The stub answers `{}`. That satisfies the spec for the first, breaks it for the second.
MATCHES = "PATCH /conversations/{conversationId}/archive"
DIFFERS = "GET /search/{searchId}"
SKIPPED = "DELETE /search"
TEST_MODULE = "contract/integration_test_contract.py"

ROOT_CONFTEST = """
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w

import helper.contract.config as contract_config
from helper.contract.stub_server import stub_server

HERE = Path(__file__).parent
# The stub needs no rate limit, and the test should take seconds.
base = tomllib.loads(contract_config.BASE_CONFIG_PATH.read_text())
base.pop("rate-limit")
contract_config.BASE_CONFIG_PATH = HERE / "schemathesis.base.toml"
contract_config.BASE_CONFIG_PATH.write_bytes(tomli_w.dumps(base).encode())

DELETED = HERE / "deleted.txt"


class Response:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self):
        return self._body


def conversation(conversation_id):
    messages = [{"_id": f"{conversation_id}-bot", "messageType": "bot_response"}]
    return Response(201, {"conversation": {"_id": conversation_id, "messages": messages}})


class Conversations:
    count = 0

    def create_conversation(self, *args, **kwargs):
        Conversations.count += 1
        return conversation(f"conversation-{Conversations.count}")

    def delete_conversation(self, *ids):
        with DELETED.open("a") as log:
            log.write("conversation " + " ".join(ids) + "\\n")
        return Response(200, {})

    def archive_conversation(self, *ids):
        return Response(200, {})


class Agents:
    def create_agent(self, **payload):
        return Response(201, {"agent": {"_key": "agent-" + payload["name"].split("-")[1]}})

    def delete_agent(self, key):
        return Response(404, {})


class Searches:
    def search(self, query, **kwargs):
        if SEARCH_FAILS:
            return Response(404, {"error": "No documents are available"})
        return Response(200, {"searchId": "search-" + query.split()[1]})

    def delete_search(self, search_id):
        return Response(200, {})

    def archive_search(self, search_id):
        return Response(200, {})


class Projects:
    def create_project(self, name):
        return Response(201, {"project": {"_id": "project-1"}})

    def delete_project(self, project_id):
        return Response(200, {})


@pytest.fixture(scope="session")
def pipeshub_client():
    with stub_server() as url:
        yield SimpleNamespace(base_url=url, request=lambda method, path: Response(200, {}))


@pytest.fixture(scope="session")
def conversations_client():
    return Conversations()


@pytest.fixture(scope="session")
def agent_conversations_client():
    return Conversations()


@pytest.fixture(scope="session")
def agents_client():
    return Agents()


@pytest.fixture(scope="session")
def search_client():
    return Searches()


@pytest.fixture(scope="session")
def projects_client():
    return Projects()


@pytest.fixture(scope="session")
def session_kb():
    return {"kb_id": "kb-1", "record_id": "record-1"}


@pytest.fixture(scope="session")
def reasoning_multimodal_llm_model():
    return SimpleNamespace(model_key="key-1", model_name="model", provider="openAI")


@pytest.fixture(scope="session")
def agent_session():
    return {"workhorse_agent": "agent-main"}


@pytest.fixture(scope="session")
def readonly_conversation():
    return {"conversation_id": "conversation-readonly", "bot_message_id": "message-readonly"}


@pytest.fixture(scope="session")
def readonly_agent_conversation():
    return {"conversation_id": "agent-conversation-readonly", "bot_message_id": "message-readonly"}


@pytest.fixture(scope="session")
def second_user():
    return SimpleNamespace(user_id="user-2")
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    contract = tmp_path / "contract"
    shutil.copytree(CONTRACT_DIR, contract, ignore=shutil.ignore_patterns("__pycache__"))
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
    assert "API contract: enterprise search" in output
    assert "Contract: DIFFERS — " in output
    assert f"Skipped: {SKIPPED} — Deletes all search history" in output

    # Only the selected operations were sent, with the values from the fixtures.
    manifest = (project / "reports/enterprise-search/run/manifest.json").read_text(encoding="utf-8")
    assert manifest.count('"state": "full"') == 2
    config = (project / "reports/enterprise-search/run/schemathesis.toml").read_text(
        encoding="utf-8"
    )
    assert '"path.searchId" = "search-readonly"' in config
    assert '"path.conversationId" = "conversation-2"' in config, "the archivable conversation"

    # The fixture conversations are deleted at the end.
    deleted = (project / "deleted.txt").read_text(encoding="utf-8")
    assert deleted.count("conversation ") == 8


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
