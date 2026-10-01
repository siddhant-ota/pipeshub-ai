"""Fixtures for the enterprise-search contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`conversation.mutable.id`, `knowledgeBase.id`, ...).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from helper import kb_sharing
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
PINNED = "pinned"
CONVERSATION_ROLES = ("mutable", "archivable", ARCHIVED, "disposable")
AGENT_ROLES = ("mutable", "disposable")
SEARCH_ROLES = ("readonly", "archivable", ARCHIVED, "disposable")
# `pinnable` is there to be pinned; `pinned` already is, to be unpinned. `kbHost` gets its
# hidden knowledge base from the operation under test.
PROJECT_ROLES = (
    "readonly",
    "mutable",
    "archivable",
    ARCHIVED,
    "pinnable",
    PINNED,
    "disposable",
    "kbHost",
)
# The same three roles for the member list of a project. `membersMutable` starts with no member.
MEMBERS_MUTABLE = "membersMutable"
MEMBER_PROJECT_ROLES = ("membersReadonly", MEMBERS_MUTABLE, "membersDisposable")
_CONVERSATION_IDS = ("id", "botMessageId")
_LLM_TIMEOUT_SEC = 180
_SPEECH_CAPABILITIES = "/api/v1/chat/speech/capabilities"
NO_PROVIDER = "none"
_PAGE_SIZE = 100
# A search with no `limit` is answered 500 after its LLM calls: the validator takes the field
# as optional (es_validators.ts) and the save needs it (search.schema.ts).
_SEARCH_LIMIT = 5

logger = logging.getLogger("contract")


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


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
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


def _create_project(projects_client: ProjectsClient, role: str) -> str:
    resp = projects_client.create_project(name=f"contract-{role}-{uuid4().hex[:8]}")
    project = response_body(resp, (201,), f"Create project ({role})").get("project") or {}
    assert project.get("_id"), f"Create project ({role}): response has no project._id"
    return str(project["_id"])


def _delete_projects(projects_client: ProjectsClient, created: dict[str, str]) -> None:
    # Deleting a project also deletes its hidden knowledge base (project.controller.ts).
    for role, project_id in created.items():
        delete_quietly(
            f"project ({role})",
            lambda project_id=project_id: projects_client.delete_project(project_id),
        )


def _delete_conversations_in(
    project_id: str,
    projects_client: ProjectsClient,
    conversations_client: ConversationsClient,
    agent_conversations_client: AgentConversationsClient,
) -> None:
    """Delete the conversations that the test cases left in the project.

    A stream operation saves its conversation and answers 200 before it calls the LLM, and a
    create whose LLM call fails keeps its conversation. Neither answer has the ID in a JSON
    body, so `created_resources` cannot find the conversation. Every such request that the
    API can accept has `projectId`, so the conversation is in this project. The one request
    without it is the example of `createAgentConversation`.

    A `mutable` fixture conversation is here too if the operation under test linked it. Its
    own fixture then gets a 404 for its delete, which `delete_quietly` takes as done.
    """
    tried: set[str] = set()
    while True:
        try:
            resp = projects_client.list_project_conversations(project_id, limit=_PAGE_SIZE)
            rows = response_body(resp, (200,), "List project conversations").get("conversations")
        except (AssertionError, requests.RequestException, ValueError) as exc:
            logger.warning("Could not list the conversations of project %s: %s", project_id, exc)
            return
        left = [row for row in rows or [] if str(row.get("_id")) not in tried]
        if not left:
            return
        for row in left:
            conversation_id, agent_key = str(row.get("_id")), row.get("agentKey")
            tried.add(conversation_id)
            # DELETE /conversations/{id} finds no agent conversation (es_controller.ts).
            delete_quietly(
                f"conversation {conversation_id} in the project",
                lambda conversation_id=conversation_id, agent_key=agent_key: (
                    agent_conversations_client.delete_conversation(agent_key, conversation_id)
                    if agent_key
                    else conversations_client.delete_conversation(conversation_id)
                ),
            )


@pytest.fixture(scope="module")
def contract_project(
    projects_client: ProjectsClient,
    conversations_client: ConversationsClient,
    agent_conversations_client: AgentConversationsClient,
) -> Iterator[str]:
    project_id = _create_project(projects_client, "linked")
    try:
        yield project_id
    finally:
        # Before the project: its delete takes every conversation out of it (project.service.ts).
        _delete_conversations_in(
            project_id, projects_client, conversations_client, agent_conversations_client
        )
        _delete_projects(projects_client, {"linked": project_id})


@pytest.fixture(scope="module")
def contract_projects(projects_client: ProjectsClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in PROJECT_ROLES:
            created[role] = _create_project(projects_client, role)
        response_body(projects_client.archive_project(created[ARCHIVED]), (200,), "Archive project")
        response_body(projects_client.pin_project(created[PINNED]), (200,), "Pin project")
        yield created
    finally:
        _delete_projects(projects_client, created)


@pytest.fixture(scope="module")
def contract_member_projects(
    projects_client: ProjectsClient, second_user: Any
) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in MEMBER_PROJECT_ROLES:
            created[role] = _create_project(projects_client, role)
            if role == MEMBERS_MUTABLE:
                continue
            # The projects API names a user by the Mongo ID, as IAM does, not by the graph ID.
            resp = projects_client.upsert_members(
                created[role], [{"principalId": second_user.user_id, "role": "viewer"}]
            )
            what = f"Add the second user to the project ({role})"
            assert response_body(resp, (200,), what).get("members"), f"{what}: no member"
        yield created
    finally:
        _delete_projects(projects_client, created)


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
def contract_searches(search_client: SearchClient, session_kb: Any) -> Iterator[dict[str, str]]:
    del session_kb  # a search is refused until a document is indexed
    created: dict[str, str] = {}
    try:
        for role in SEARCH_ROLES:
            resp = search_client.search(
                f"contract {role} {uuid4().hex[:8]}", limit=_SEARCH_LIMIT, timeout=_LLM_TIMEOUT_SEC
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


def _as_second_user(user: Any, method: str, path: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, f"{user.base_url}{path}", headers=user.headers, **kwargs)


@pytest.fixture(scope="module")
def contract_second_user_history(
    pipeshub_client: Any, session_kb: Any, second_user: Any
) -> Iterator[str]:
    """The session token of the second user, who has one search of their own.

    `DELETE /search` deletes every search of its caller. Sent as the second user, it
    finds this search and cannot reach the search fixtures of the other operations.
    """
    kb_id = session_kb["kb_id"]
    # A user can search only a knowledge base that they can read.
    kb_sharing.grant(pipeshub_client, kb_id, user_ids=[second_user.user_id])
    try:
        resp = _as_second_user(
            second_user,
            "POST",
            "/api/v1/search",
            json={
                "query": f"contract history {uuid4().hex[:8]}",
                "filters": {"kb": [kb_id]},
                "limit": _SEARCH_LIMIT,
            },
            timeout=_LLM_TIMEOUT_SEC,
        )
    finally:
        kb_sharing.revoke(pipeshub_client, kb_id, user_ids=[second_user.user_id], strict=False)
    search_id = response_body(resp, (200,), "Search as the second user").get("searchId")
    assert search_id, "Search as the second user: response has no searchId"
    try:
        yield second_user.token
    finally:
        delete_quietly(
            "search of the second user",
            lambda: _as_second_user(
                second_user, "DELETE", f"/api/v1/search/{search_id}", timeout=second_user.timeout
            ),
        )


@pytest.fixture(scope="module")
def contract_attachment_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small text file to attach to a chat.

    Text, not a PDF or a picture: the upload has a model describe every image of an
    attachment (sink_orchestrator.py), and a text file has none.
    """
    path = tmp_path_factory.mktemp("contract") / f"contract-attachment-{uuid4().hex[:8]}.txt"
    path.write_text("A text file of the API contract tests.\n", encoding="utf-8")
    return path


def _files_part(path: Path) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (path.name, path.read_bytes(), "text/plain"))]


def _attachment_id(resp: requests.Response, what: str) -> str:
    attachments = response_body(resp, (200,), what).get("attachments") or []
    assert attachments and attachments[0].get("recordId"), (
        f"{what}: response has no attachments[0].recordId"
    )
    return str(attachments[0]["recordId"])


@pytest.fixture(scope="module")
def contract_attachment(
    conversations_client: ConversationsClient, contract_attachment_file: Path
) -> Iterator[str]:
    resp = conversations_client.post(
        "/attachments/upload", files=_files_part(contract_attachment_file)
    )
    record_id = _attachment_id(resp, "Upload chat attachment")
    try:
        yield record_id
    finally:
        delete_quietly(
            "chat attachment", lambda: conversations_client.delete(f"/attachments/{record_id}")
        )


@pytest.fixture(scope="module")
def contract_agent_attachment(
    agent_conversations_client: AgentConversationsClient,
    agent_session: Any,
    contract_attachment_file: Path,
) -> Iterator[str]:
    agent_key = agent_session["workhorse_agent"]
    resp = agent_conversations_client.upload_attachments(
        agent_key, files=_files_part(contract_attachment_file)
    )
    record_id = _attachment_id(resp, "Upload agent chat attachment")
    try:
        yield record_id
    finally:
        delete_quietly(
            "agent chat attachment",
            lambda: agent_conversations_client.delete_attachment(agent_key, record_id),
        )


@pytest.fixture(scope="module")
def contract_speech_capabilities(pipeshub_client: Any) -> dict[str, Any]:
    return response_body(
        pipeshub_client.request("GET", _SPEECH_CAPABILITIES), (200,), "Speech capabilities"
    )


def _no_provider(capabilities: dict[str, Any], kind: str, name: str) -> str:
    """`NO_PROVIDER`, or a skip of the operation: this deployment has a provider.

    With a provider, the requests of the run would call it, also the invalid ones. The API
    does not reject an unknown `format` or a `speed` out of range; it takes mp3 and clamps
    the speed, and then calls the provider.
    """
    if capabilities.get(kind) is not None:
        pytest.skip(
            f"A {name} provider is configured on this deployment. Requests to the operation "
            "would call it, and that costs money."
        )
    return NO_PROVIDER


@pytest.fixture(scope="module")
def contract_no_tts_provider(contract_speech_capabilities: dict[str, Any]) -> str:
    return _no_provider(contract_speech_capabilities, "tts", "text-to-speech")


@pytest.fixture(scope="module")
def contract_no_stt_provider(contract_speech_capabilities: dict[str, Any]) -> str:
    return _no_provider(contract_speech_capabilities, "stt", "speech-to-text")


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
        "One project, to link conversations to. The project operations only read it. At the "
        "end the fixture deletes every conversation that is in it, also those that test "
        "cases left there.",
    ),
    ValueSource(
        "contract_projects",
        tuple(f"project.{role}.id" for role in PROJECT_ROLES),
        lambda projects: tuple(projects[role] for role in PROJECT_ROLES),
        "Eight projects: to read, to update, to archive, already archived (to unarchive), "
        "to pin, already pinned (to unpin), to delete, and one that gets its hidden knowledge "
        "base from the operation under test.",
    ),
    ValueSource(
        "contract_member_projects",
        tuple(f"project.{role}.id" for role in MEMBER_PROJECT_ROLES),
        lambda projects: tuple(projects[role] for role in MEMBER_PROJECT_ROLES),
        "Three projects for the member operations: one with the second user as a viewer, to "
        "list; one with no member, to add to; one with the second user as a viewer, to remove.",
    ),
    ValueSource(
        "second_user",
        ("user.second.id",),
        lambda user: (user.user_id,),
        "A second user of the organization, to share a conversation with and to be a member "
        "of a project.",
        added=False,
    ),
    ValueSource(
        "contract_second_user_history",
        ("user.second.token",),
        lambda token: (token,),
        "The session token of the second user, after one search as that user, so that the "
        "delete of the search history has a search of its own to delete. For that one search "
        "the second user can read the knowledge base.",
        secret=True,
    ),
    ValueSource(
        "contract_attachment_file",
        ("attachment.file.path",),
        lambda path: (str(path),),
        "A small text file in a temporary folder of the test run, to upload as a chat "
        "attachment. The fixture creates nothing on the deployment. Each upload of the file "
        "leaves a stored copy there: the delete of an attachment removes its record, not "
        "the file in the storage.",
    ),
    ValueSource(
        "contract_attachment",
        ("attachment.disposable.id",),
        lambda record_id: (record_id,),
        "One chat attachment, the text file uploaded through the assistant route, to delete. "
        "Its stored file stays in the storage.",
    ),
    ValueSource(
        "contract_agent_attachment",
        ("agentAttachment.disposable.id",),
        lambda record_id: (record_id,),
        "One chat attachment, the text file uploaded through the agent route, to delete. "
        "Its stored file stays in the storage.",
    ),
    ValueSource(
        "contract_no_tts_provider",
        ("speech.tts.provider",),
        lambda provider: (provider,),
        "Reads the speech capabilities. If a text-to-speech provider is configured, the "
        "operation is skipped, so that no request of the run can call the provider. It "
        "creates nothing.",
    ),
    ValueSource(
        "contract_no_stt_provider",
        ("speech.stt.provider",),
        lambda provider: (provider,),
        "Reads the speech capabilities. If a speech-to-text provider is configured, the "
        "operation is skipped, so that no request of the run can call the provider. It "
        "creates nothing.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
