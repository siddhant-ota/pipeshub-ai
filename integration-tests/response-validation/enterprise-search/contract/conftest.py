"""Fixtures for the enterprise-search contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`conversation.mutable.id`, `knowledgeBase.id`, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
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
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.conversation_seeds import seed_query

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# One resource per role, so that an update, an archive or a delete under test
# cannot change what another operation reads, in whatever order they run.
# `archivable` is there to be archived; `archived` already is, to be unarchived.
ARCHIVED = "archived"
CONVERSATION_ROLES = ("mutable", "archivable", ARCHIVED, "disposable")
AGENT_ROLES = ("mutable", "disposable")
SEARCH_ROLES = ("readonly", "archivable", ARCHIVED, "disposable")
_CONVERSATION_IDS = ("id", "botMessageId")
_LLM_TIMEOUT_SEC = 180


def _conversation_ids(resp: requests.Response, what: str) -> dict[str, str]:
    conversation = response_body(resp, (200, 201), what).get("conversation") or {}
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


def _seed(role: str) -> str:
    return seed_query(f"contract-{role}-{uuid4().hex[:8]}")


@pytest.fixture(scope="session")
def contract_conversations(
    conversations_client: ConversationsClient, ai_models_configured: Any
) -> Iterator[dict[str, dict[str, str]]]:
    del ai_models_configured  # a conversation needs an LLM
    created: dict[str, dict[str, str]] = {}
    try:
        for role in CONVERSATION_ROLES:
            resp = conversations_client.create_conversation(
                query=_seed(role), timeout=_LLM_TIMEOUT_SEC
            )
            created[role] = _conversation_ids(resp, f"Create conversation ({role})")
        response_body(
            conversations_client.archive_conversation(created[ARCHIVED]["id"]),
            (200,),
            "Archive conversation",
        )
        yield created
    finally:
        for role, ids in created.items():
            delete_quietly(
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
            agent = response_body(resp, (200, 201), f"Create agent ({role})").get("agent") or {}
            assert agent.get("_key"), f"Create agent ({role}): response has no agent._key"
            created[role] = str(agent["_key"])
        yield created
    finally:
        for role, key in created.items():
            delete_quietly(f"agent ({role})", lambda key=key: agents_client.delete_agent(key))


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
                agent_key, query=_seed(f"agent-{role}"), timeout=_LLM_TIMEOUT_SEC
            )
            created[role] = _conversation_ids(resp, f"Create agent conversation ({role})")
        response_body(
            agent_conversations_client.archive_conversation(agent_key, created[ARCHIVED]["id"]),
            (200,),
            "Archive agent conversation",
        )
        yield created
    finally:
        for role, ids in created.items():
            delete_quietly(
                f"agent conversation ({role})",
                lambda ids=ids: agent_conversations_client.delete_conversation(
                    agent_key, ids["id"]
                ),
            )


@pytest.fixture(scope="session")
def contract_project(projects_client: ProjectsClient) -> Iterator[str]:
    resp = projects_client.create_project(name=f"contract-{uuid4().hex[:8]}")
    project = response_body(resp, (201,), "Create project").get("project") or {}
    assert project.get("_id"), "Create project: response has no project._id"
    try:
        yield str(project["_id"])
    finally:
        delete_quietly("project", lambda: projects_client.delete_project(project["_id"]))


@pytest.fixture(scope="session")
def contract_linked_conversations(
    conversations_client: ConversationsClient,
    agent_conversations_client: AgentConversationsClient,
    agent_session: Any,
    contract_project: str,
    ai_models_configured: Any,
) -> Iterator[dict[str, str]]:
    """A conversation and an agent conversation that are already in a project.

    The project-visibility operations answer 400 for a conversation that is in
    no project, so they get their own, linked here and not by another operation.
    """
    del ai_models_configured
    agent_key = agent_session["workhorse_agent"]
    created: dict[str, str] = {}
    try:
        resp = conversations_client.create_conversation(
            query=_seed("linked"), timeout=_LLM_TIMEOUT_SEC
        )
        created["conversation"] = _conversation_ids(resp, "Create conversation (linked)")["id"]
        response_body(
            conversations_client.set_project(created["conversation"], contract_project),
            (200,),
            "Link conversation to project",
        )
        resp = agent_conversations_client.create_conversation(
            agent_key, query=_seed("agent-linked"), timeout=_LLM_TIMEOUT_SEC
        )
        created["agentConversation"] = _conversation_ids(
            resp, "Create agent conversation (linked)"
        )["id"]
        response_body(
            agent_conversations_client.set_project(
                agent_key, created["agentConversation"], contract_project
            ),
            (200,),
            "Link agent conversation to project",
        )
        yield created
    finally:
        if "conversation" in created:
            delete_quietly(
                "conversation (linked)",
                lambda: conversations_client.delete_conversation(created["conversation"]),
            )
        if "agentConversation" in created:
            delete_quietly(
                "agent conversation (linked)",
                lambda: agent_conversations_client.delete_conversation(
                    agent_key, created["agentConversation"]
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
            search_id = response_body(resp, (200, 201), f"Create search ({role})").get("searchId")
            assert search_id, f"Create search ({role}): response has no searchId"
            created[role] = str(search_id)
        response_body(search_client.archive_search(created[ARCHIVED]), (200,), "Archive search")
        yield created
    finally:
        for role, search_id in created.items():
            # DELETE /search/{id} finds only a search that is not archived, and the archive
            # operation under test archives one more. Unarchiving one that is not archived is a 404.
            delete_quietly(
                f"search ({role}), unarchive",
                lambda search_id=search_id: search_client.unarchive_search(search_id),
            )
            delete_quietly(
                f"search ({role})",
                lambda search_id=search_id: search_client.delete_search(search_id),
            )


def _by_role(fixture: str, prefix: str, roles: tuple[str, ...], what: str) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.{name}" for role in roles for name in _CONVERSATION_IDS),
        lambda by_role: tuple(by_role[role][name] for role in roles for name in _CONVERSATION_IDS),
        what,
    )


def _readonly(fixture: str, prefix: str, what: str) -> ValueSource:
    return ValueSource(
        fixture,
        (f"{prefix}.readonly.id", f"{prefix}.readonly.botMessageId"),
        lambda conversation: (conversation["conversation_id"], conversation["bot_message_id"]),
        what,
        added=False,
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "session_kb",
        ("knowledgeBase.id", "knowledgeBase.recordId"),
        lambda kb: (kb["kb_id"], kb["record_id"]),
        "A knowledge base with one indexed PDF. The record ID is that PDF.",
        added=False,
    ),
    ValueSource(
        "reasoning_multimodal_llm_model",
        ("llm.modelKey", "llm.modelName", "llm.provider"),
        lambda model: (model.model_key, model.model_name, model.provider),
        "An LLM that the fixture adds to the AI model configuration.",
        added=False,
    ),
    ValueSource(
        "agent_session",
        ("agent.main.key",),
        lambda agents: (agents["workhorse_agent"],),
        "The agent without knowledge (`workhorse_agent`). It owns the agent conversations.",
        added=False,
    ),
    ValueSource(
        "contract_agents",
        tuple(f"agent.{role}.key" for role in AGENT_ROLES),
        lambda agents: tuple(agents[role] for role in AGENT_ROLES),
        "Two agents: one to update, one to delete.",
    ),
    _readonly(
        "readonly_conversation",
        "conversation",
        "A conversation with one answer, which no operation changes.",
    ),
    _by_role(
        "contract_conversations",
        "conversation",
        CONVERSATION_ROLES,
        "Four conversations with one answer each: to update, to archive, already archived "
        "(to unarchive), and to delete.",
    ),
    _readonly(
        "readonly_agent_conversation",
        "agentConversation",
        "A conversation with the main agent, which no operation changes.",
    ),
    _by_role(
        "contract_agent_conversations",
        "agentConversation",
        CONVERSATION_ROLES,
        "Four conversations with the main agent: to update, to archive, already archived "
        "(to unarchive), and to delete.",
    ),
    ValueSource(
        "contract_linked_conversations",
        ("conversation.linked.id", "agentConversation.linked.id"),
        lambda linked: (linked["conversation"], linked["agentConversation"]),
        "A conversation and an agent conversation that are already in the project, for the "
        "project-visibility operations.",
    ),
    ValueSource(
        "contract_searches",
        tuple(f"search.{role}.id" for role in SEARCH_ROLES),
        lambda searches: tuple(searches[role] for role in SEARCH_ROLES),
        "Four saved searches: to read, to archive, already archived (to unarchive), and to delete.",
    ),
    ValueSource(
        "contract_project",
        ("project.id",),
        lambda project_id: (project_id,),
        "One project, to link conversations to.",
    ),
    ValueSource(
        "second_user",
        ("user.second.id",),
        lambda user: (user.user_id,),
        "A second user of the organization, to share a conversation with.",
        added=False,
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
