"""Fixture data for the contract tests: real IDs for the path parameters.

Each role exists so that one group of operations cannot break another:
`readonly` is only read, `mutable` is updated, `archivable` is archived and
unarchived, and `disposable` is deleted by the DELETE operation under test.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from env import base_url, log_in
from events import API_PREFIX, read_cases
from suite import Suite

from helper.clients.agents_client import AgentsClient
from helper.clients.ai_models_client import AIModelsClient
from helper.clients.conversations_client import (
    AgentConversationsClient,
    ConversationsClient,
)
from helper.clients.search_client import SearchClient
from helper.conversation_seeds import seed_query
from helper.http.session_client import SessionClient

logger = logging.getLogger("contract-fixtures")

CONVERSATION_ROLES = ("readonly", "mutable", "archivable", "disposable")
AGENT_ROLES = ("main", "mutable", "disposable")
SEARCH_ROLES = ("readonly", "archivable", "disposable")

_LLM_TIMEOUT_SEC = 180
_PLACEHOLDER_ID = "0" * 24


class FixtureError(Exception):
    """One fixture could not be created; the fixtures that need it are left out."""


@dataclass
class Fixtures:
    values: dict[str, str] = field(default_factory=dict)
    # (kind, id, agent key or "") for cleanup, in creation order
    created: list[tuple[str, str, str]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"values": self.values, "created": self.created, "errors": self.errors},
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Fixtures:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            values=raw.get("values") or {},
            created=[tuple(entry) for entry in raw.get("created") or []],
            errors=raw.get("errors") or {},
        )


def placeholder_fixtures(keys: set[str]) -> dict[str, str]:
    """Stand-in IDs for `plan`, which sends nothing to a real PipesHub."""
    return dict.fromkeys(keys, _PLACEHOLDER_ID)


def _session() -> SessionClient:
    return SessionClient(base_url(), login=log_in)


def _json_or_raise(resp: requests.Response, expected: tuple[int, ...], what: str) -> dict[str, Any]:
    if resp.status_code not in expected:
        raise FixtureError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise FixtureError(f"{what}: response is not JSON") from exc
    if not isinstance(body, dict):
        raise FixtureError(f"{what}: expected a JSON object, got {type(body).__name__}")
    return body


def _conversation_ids(body: dict[str, Any], what: str) -> dict[str, str]:
    conversation = body.get("conversation")
    if not isinstance(conversation, dict) or not conversation.get("_id"):
        raise FixtureError(f"{what}: response has no conversation._id")
    ids = {"id": str(conversation["_id"])}
    for message in conversation.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_id = message.get("_id") or message.get("id")
        if message.get("messageType") == "bot_response" and message_id:
            ids["botMessageId"] = str(message_id)
        elif message.get("messageType") == "user_query" and message_id:
            ids.setdefault("userMessageId", str(message_id))
    if "botMessageId" not in ids:
        raise FixtureError(f"{what}: conversation has no bot_response message")
    return ids


def _first_llm(session: SessionClient) -> dict[str, Any]:
    body = _json_or_raise(
        AIModelsClient(session).get_models_by_type("llm"), (200,), "List LLM models"
    )
    models = [model for model in body.get("models") or [] if isinstance(model, dict)]
    if not models:
        raise FixtureError(
            "No LLM is configured on this deployment. Conversations and agents need one."
        )
    # Agents need a reasoning model; fall back to the first model if none is marked.
    return next((model for model in models if model.get("isReasoning")), models[0])


def _agent_payload(name: str, model: dict[str, Any]) -> dict[str, Any]:
    configuration = model.get("configuration") if isinstance(model.get("configuration"), dict) else {}
    model_name = str(configuration.get("model") or model.get("modelName") or model["modelKey"])
    return {
        "name": name,
        "models": [
            {
                "modelKey": model["modelKey"],
                "modelName": model_name.split(",")[0].strip(),
                "provider": model.get("provider"),
                "isReasoning": True,
            }
        ],
    }


def _attempt(fixtures: Fixtures, group: str, build: Callable[[], None]) -> None:
    try:
        build()
    except (FixtureError, requests.RequestException) as exc:
        fixtures.errors[group] = str(exc)
        logger.warning("Fixture group %s failed: %s", group, exc)


def create_fixtures() -> Fixtures:
    """Create every fixture that this deployment allows; record what failed and why."""
    session = _session()
    fixtures = Fixtures()
    run = uuid4().hex[:8]

    def _conversations() -> None:
        client = ConversationsClient(session)
        for role in CONVERSATION_ROLES:
            what = f"Create conversation ({role})"
            resp = client.create_conversation(
                query=seed_query(f"contract-{run}-{role}"), timeout=_LLM_TIMEOUT_SEC
            )
            ids = _conversation_ids(_json_or_raise(resp, (200, 201), what), what)
            fixtures.created.append(("conversation", ids["id"], ""))
            for name, value in ids.items():
                fixtures.values[f"conversation.{role}.{name}"] = value

    def _agents() -> None:
        model = _first_llm(session)
        client = AgentsClient(session)
        for role in AGENT_ROLES:
            what = f"Create agent ({role})"
            resp = client.create_agent(**_agent_payload(f"contract-{run}-{role}", model))
            agent = _json_or_raise(resp, (200, 201), what).get("agent") or {}
            key = agent.get("_key")
            if not key:
                raise FixtureError(f"{what}: response has no agent._key")
            fixtures.created.append(("agent", str(key), ""))
            fixtures.values[f"agent.{role}.key"] = str(key)

    def _agent_conversations() -> None:
        agent_key = fixtures.values.get("agent.main.key")
        if not agent_key:
            raise FixtureError("Needs agent.main.key, which was not created.")
        client = AgentConversationsClient(session)
        for role in CONVERSATION_ROLES:
            what = f"Create agent conversation ({role})"
            resp = client.create_conversation(
                agent_key,
                query=seed_query(f"contract-{run}-agent-{role}"),
                timeout=_LLM_TIMEOUT_SEC,
            )
            ids = _conversation_ids(_json_or_raise(resp, (200, 201), what), what)
            fixtures.created.append(("agentConversation", ids["id"], agent_key))
            for name, value in ids.items():
                fixtures.values[f"agentConversation.{role}.{name}"] = value

    def _searches() -> None:
        client = SearchClient(session)
        for role in SEARCH_ROLES:
            what = f"Create search ({role})"
            resp = client.search(f"contract {run} {role}", timeout=_LLM_TIMEOUT_SEC)
            search_id = _json_or_raise(resp, (200, 201), what).get("searchId")
            if not search_id:
                raise FixtureError(f"{what}: response has no searchId")
            fixtures.created.append(("search", str(search_id), ""))
            fixtures.values[f"search.{role}.id"] = str(search_id)

    _attempt(fixtures, "conversation", _conversations)
    _attempt(fixtures, "agent", _agents)
    _attempt(fixtures, "agentConversation", _agent_conversations)
    _attempt(fixtures, "search", _searches)
    return fixtures


def _at_pointer(document: Any, pointer: str) -> Any:
    for part in pointer.strip("/").split("/"):
        if not isinstance(document, dict):
            return None
        document = document.get(part)
    return document


def delete_created_by_tests(
    suite: Suite, fixtures: Fixtures, ndjson_path: Path
) -> tuple[int, list[str]]:
    """Delete what the test cases themselves created, for example agents from `createAgent`.

    Returns the number of resources found and the failures; a 404 is not one.
    """
    rules = {rule.operation.label: rule for rule in suite.created_resources}
    paths: list[str] = []
    for case in read_cases(ndjson_path):
        rule = rules.get(case.label)
        if rule is None or case.status is None or not 200 <= case.status < 300:
            continue
        resource_id = _at_pointer(case.response_json(), rule.id_pointer)
        if not isinstance(resource_id, str) or not resource_id:
            continue
        path = rule.delete_path.replace("{id}", resource_id)
        for key, value in fixtures.values.items():
            path = path.replace(f"{{{key}}}", value)
        paths.append(path)

    session = _session()
    failures: list[str] = []
    for path in paths:
        if "{" in path:
            failures.append(f"{path}: a fixture key in the path has no value")
            continue
        try:
            resp = session.request("DELETE", f"{API_PREFIX}{path}")
        except requests.RequestException as exc:
            failures.append(f"{path}: {exc}")
            continue
        if resp.status_code >= 400 and resp.status_code != 404:
            failures.append(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
    return len(paths), failures


def delete_fixtures(fixtures: Fixtures) -> list[str]:
    """Delete what `create_fixtures` made. Returns the failures; a 404 is not one."""
    session = _session()
    conversations = ConversationsClient(session)
    agent_conversations = AgentConversationsClient(session)
    agents = AgentsClient(session)
    searches = SearchClient(session)
    failures: list[str] = []

    # Agent conversations go before their agent.
    order = {"agentConversation": 0, "conversation": 1, "search": 2, "agent": 3}
    for kind, resource_id, agent_key in sorted(fixtures.created, key=lambda e: order[e[0]]):
        try:
            if kind == "conversation":
                resp = conversations.delete_conversation(resource_id)
            elif kind == "agentConversation":
                resp = agent_conversations.delete_conversation(agent_key, resource_id)
            elif kind == "agent":
                resp = agents.delete_agent(resource_id)
            else:
                resp = searches.delete_search(resource_id)
        except requests.RequestException as exc:
            failures.append(f"{kind} {resource_id}: {exc}")
            continue
        if resp.status_code >= 400 and resp.status_code != 404:
            failures.append(f"{kind} {resource_id}: HTTP {resp.status_code} {resp.text[:200]}")
    return failures
