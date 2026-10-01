"""Fixtures for the enterprise-search contract tests.

`contract_values` turns the integration-test fixtures into the values that
`suite.yaml` names (`conversation.mutable.id`, `knowledgeBase.id`, ...).
`contract_run` sends the suite's test cases once and judges the answers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from helper.clients.agents_client import AgentsClient
from helper.clients.conversations_client import (
    AgentConversationsClient,
    ConversationsClient,
)
from helper.clients.projects_client import ProjectsClient
from helper.clients.search_client import SearchClient
from helper.contract.created import created_resource_paths
from helper.contract.events import API_PREFIX
from helper.contract.report import summary_lines
from helper.contract.runner import ContractRun, RunnerError, execute, judge, run_files
from helper.contract.suite import load_suite
from helper.contract.values import ContractValues
from helper.conversation_seeds import seed_query

logger = logging.getLogger("enterprise-search-contract")

SUITE_PATH = Path(__file__).with_name("suite.yaml")
BASELINE_PATH = Path(__file__).with_name("baseline.json")
TEST_FILE = "integration_test_contract.py"

# One resource per role, so that an update, an archive or a delete under test
# cannot change what another operation reads, in whatever order they run.
# `archivable` is there to be archived; `archived` already is, to be unarchived.
ARCHIVED = "archived"
CONVERSATION_ROLES = ("mutable", "archivable", ARCHIVED, "disposable")
AGENT_ROLES = ("mutable", "disposable")
SEARCH_ROLES = ("readonly", "archivable", ARCHIVED, "disposable")
_CONVERSATION_IDS = ("id", "botMessageId")
_LLM_TIMEOUT_SEC = 180


def _body(resp: requests.Response, expected: tuple[int, ...], what: str) -> dict[str, Any]:
    assert resp.status_code in expected, f"{what}: HTTP {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, dict), f"{what}: expected a JSON object, got {body!r}"
    return body


def _conversation_ids(resp: requests.Response, what: str) -> dict[str, str]:
    conversation = _body(resp, (200, 201), what).get("conversation") or {}
    assert conversation.get("_id"), f"{what}: response has no conversation._id"
    bot_message = next(
        (
            message
            for message in reversed(conversation.get("messages") or [])
            if isinstance(message, dict) and message.get("messageType") == "bot_response"
        ),
        None,
    )
    assert bot_message and bot_message.get("_id"), (
        f"{what}: conversation has no bot_response message"
    )
    return {"id": str(conversation["_id"]), "botMessageId": str(bot_message["_id"])}


def _delete_quietly(what: str, delete: Callable[[], requests.Response]) -> None:
    try:
        resp = delete()
    except requests.RequestException as exc:
        logger.warning("Could not delete %s: %s", what, exc)
        return
    # 404: the DELETE operation under test already removed it.
    if resp.status_code >= 400 and resp.status_code != 404:
        logger.warning("Could not delete %s: HTTP %s %s", what, resp.status_code, resp.text[:200])


@pytest.fixture(scope="session")
def contract_conversations(
    conversations_client: ConversationsClient,
) -> Iterator[dict[str, dict[str, str]]]:
    created: dict[str, dict[str, str]] = {}
    try:
        for role in CONVERSATION_ROLES:
            resp = conversations_client.create_conversation(
                query=seed_query(f"contract-{role}-{uuid4().hex[:8]}"), timeout=_LLM_TIMEOUT_SEC
            )
            created[role] = _conversation_ids(resp, f"Create conversation ({role})")
        _body(
            conversations_client.archive_conversation(created[ARCHIVED]["id"]),
            (200,),
            "Archive conversation",
        )
        yield created
    finally:
        for role, ids in created.items():
            _delete_quietly(
                f"conversation ({role})",
                lambda ids=ids: conversations_client.delete_conversation(ids["id"]),
            )


@pytest.fixture(scope="session")
def contract_agents(
    agents_client: AgentsClient, reasoning_multimodal_llm_model: Any
) -> Iterator[dict[str, str]]:
    model = reasoning_multimodal_llm_model
    created: dict[str, str] = {}
    try:
        for role in AGENT_ROLES:
            resp = agents_client.create_agent(
                name=f"contract-{role}-{uuid4().hex[:8]}",
                models=[
                    {
                        "modelKey": model.model_key,
                        "modelName": model.model_name,
                        "provider": model.provider,
                        "isReasoning": True,
                    }
                ],
            )
            agent = _body(resp, (200, 201), f"Create agent ({role})").get("agent") or {}
            assert agent.get("_key"), f"Create agent ({role}): response has no agent._key"
            created[role] = str(agent["_key"])
        yield created
    finally:
        for role, key in created.items():
            _delete_quietly(f"agent ({role})", lambda key=key: agents_client.delete_agent(key))


@pytest.fixture(scope="session")
def contract_agent_conversations(
    agent_conversations_client: AgentConversationsClient, agent_session: Any
) -> Iterator[dict[str, dict[str, str]]]:
    # The knowledge-free agent: a seed query costs it one LLM turn, not a search and an answer.
    agent_key = agent_session["workhorse_agent"]
    created: dict[str, dict[str, str]] = {}
    try:
        for role in CONVERSATION_ROLES:
            resp = agent_conversations_client.create_conversation(
                agent_key,
                query=seed_query(f"contract-agent-{role}-{uuid4().hex[:8]}"),
                timeout=_LLM_TIMEOUT_SEC,
            )
            created[role] = _conversation_ids(resp, f"Create agent conversation ({role})")
        _body(
            agent_conversations_client.archive_conversation(agent_key, created[ARCHIVED]["id"]),
            (200,),
            "Archive agent conversation",
        )
        yield created
    finally:
        for role, ids in created.items():
            _delete_quietly(
                f"agent conversation ({role})",
                lambda ids=ids: agent_conversations_client.delete_conversation(
                    agent_key, ids["id"]
                ),
            )


@pytest.fixture(scope="session")
def contract_searches(search_client: SearchClient, session_kb: Any) -> Iterator[dict[str, str]]:
    del session_kb  # a search is refused until a document is indexed
    created: dict[str, str] = {}
    try:
        for role in SEARCH_ROLES:
            resp = search_client.search(
                f"contract {role} {uuid4().hex[:8]}", timeout=_LLM_TIMEOUT_SEC
            )
            search_id = _body(resp, (200, 201), f"Create search ({role})").get("searchId")
            assert search_id, f"Create search ({role}): response has no searchId"
            created[role] = str(search_id)
        _body(search_client.archive_search(created[ARCHIVED]), (200,), "Archive search")
        yield created
    finally:
        for role, search_id in created.items():
            _delete_quietly(
                f"search ({role})",
                lambda search_id=search_id: search_client.delete_search(search_id),
            )


@pytest.fixture(scope="session")
def contract_project(projects_client: ProjectsClient) -> Iterator[str]:
    resp = projects_client.create_project(name=f"contract-{uuid4().hex[:8]}")
    project = _body(resp, (201,), "Create project").get("project") or {}
    assert project.get("_id"), "Create project: response has no project._id"
    try:
        yield str(project["_id"])
    finally:
        _delete_quietly("project", lambda: projects_client.delete_project(project["_id"]))


@dataclass(frozen=True)
class ValueSource:
    """The value keys one fixture provides, and how to read them from it, in the same order."""

    fixture: str
    keys: tuple[str, ...]
    read: Callable[[Any], tuple[str, ...]]


def _by_role(fixture: str, prefix: str, roles: tuple[str, ...]) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.{name}" for role in roles for name in _CONVERSATION_IDS),
        lambda by_role: tuple(by_role[role][name] for role in roles for name in _CONVERSATION_IDS),
    )


def _readonly(fixture: str, prefix: str) -> ValueSource:
    return ValueSource(
        fixture,
        (f"{prefix}.readonly.id", f"{prefix}.readonly.botMessageId"),
        lambda conversation: (conversation["conversation_id"], conversation["bot_message_id"]),
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "session_kb",
        ("knowledgeBase.id", "knowledgeBase.recordId"),
        lambda kb: (kb["kb_id"], kb["record_id"]),
    ),
    ValueSource(
        "reasoning_multimodal_llm_model",
        ("llm.modelKey", "llm.modelName", "llm.provider"),
        lambda model: (model.model_key, model.model_name, model.provider),
    ),
    ValueSource("agent_session", ("agent.main.key",), lambda agents: (agents["workhorse_agent"],)),
    ValueSource(
        "contract_agents",
        tuple(f"agent.{role}.key" for role in AGENT_ROLES),
        lambda agents: tuple(agents[role] for role in AGENT_ROLES),
    ),
    _readonly("readonly_conversation", "conversation"),
    _by_role("contract_conversations", "conversation", CONVERSATION_ROLES),
    _readonly("readonly_agent_conversation", "agentConversation"),
    _by_role("contract_agent_conversations", "agentConversation", CONVERSATION_ROLES),
    ValueSource(
        "contract_searches",
        tuple(f"search.{role}.id" for role in SEARCH_ROLES),
        lambda searches: tuple(searches[role] for role in SEARCH_ROLES),
    ),
    ValueSource("contract_project", ("project.id",), lambda project_id: (project_id,)),
    ValueSource("second_user", ("user.second.id",), lambda user: (user.user_id,)),
)


@pytest.fixture(scope="session")
def contract_values(request: pytest.FixtureRequest) -> ContractValues:
    """Every value the suite names. A fixture that fails takes only its own values with it.

    The operations that need a missing value are not sent and their tests fail
    with the reason, while the rest of the suite still runs.
    """
    collected = ContractValues()
    for source in VALUE_SOURCES:
        try:
            values = source.read(request.getfixturevalue(source.fixture))
        # Whatever made the fixture fail, the effect here is the same: its values are missing.
        except (Exception, pytest.fail.Exception, pytest.skip.Exception) as exc:  # noqa: BLE001
            reason = f"fixture `{source.fixture}` failed: {type(exc).__name__}: {str(exc)[:300]}"
            logger.warning("Contract values: %s", reason)
            collected.missing.update(dict.fromkeys(source.keys, reason))
        else:
            collected.values.update(zip(source.keys, values, strict=True))
    return collected


def _selected_operations(session: pytest.Session) -> set[str]:
    """The operations whose tests this session runs, so that `-k` also limits what is sent."""
    return {
        item.callspec.params["operation_id"]
        for item in session.items
        if item.path.name == TEST_FILE and hasattr(item, "callspec")
    }


@pytest.fixture(scope="session")
def contract_run(
    request: pytest.FixtureRequest,
    pipeshub_client: Any,
    contract_values: ContractValues,
) -> Iterator[ContractRun]:
    suite = load_suite(SUITE_PATH)
    try:
        yield execute(
            suite,
            contract_values,
            base_url=pipeshub_client.base_url,
            baseline_path=BASELINE_PATH,
            selected=_selected_operations(request.session),
        )
    finally:
        # What the test cases themselves created, for example agents from `createAgent`.
        for path in created_resource_paths(suite, contract_values, run_files(suite).events):
            if "{" in path:
                logger.warning("Not deleted, a value in its path is missing: %s", path)
                continue
            _delete_quietly(
                path, lambda path=path: pipeshub_client.request("DELETE", f"{API_PREFIX}{path}")
            )


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Say what the run covered: a run with no failure can still have gaps by design."""
    ran = any(
        TEST_FILE in getattr(report, "nodeid", "") and getattr(report, "when", "") == "call"
        for reports in terminalreporter.stats.values()
        for report in reports
    )
    if not ran:
        return
    try:
        run = judge(load_suite(SUITE_PATH), BASELINE_PATH)
    except RunnerError:
        return
    terminalreporter.section("API contract: enterprise search")
    for line in summary_lines(list(run.results)):
        terminalreporter.write_line(line)
    terminalreporter.write_line(f"Report: {run.files.report}")
