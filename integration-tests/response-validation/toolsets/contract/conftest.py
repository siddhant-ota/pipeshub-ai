"""Fixtures for the toolsets contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`toolsetInstance.mutable.id`, `oauthConfig.disposable.id`, ...).

Every toolsets operation logs in with the session of the test user, so the
fixtures create their objects with `user_session_client`. Nothing here reaches
a third party: the API stores credentials and an OAuth client as given, and
uses them only when an agent calls a tool.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from helper.clients.agents_client import AgentsClient
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.contract.suite import load_suite
from helper.http.session_client import SessionClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

_TOOLSETS = "/api/v1/toolsets"
_INSTANCES = f"{_TOOLSETS}/instances"
# From the suite file, so that the fixtures and the `toolsetType` of the requests name the
# same toolset: the API stores OAuth configurations by toolset type.
_TOOLSET_TYPE = load_suite(SUITE_PATH).constants["toolset.type"]
_OAUTH_CONFIGS = f"{_TOOLSETS}/oauth-configs/{_TOOLSET_TYPE}"

_API_TOKEN = "API_TOKEN"
_OAUTH = "OAUTH"
_CREDENTIALS = {"auth": {"apiToken": "contract-not-a-real-token"}}
_OAUTH_CLIENT = {"clientId": "contract-client-id", "clientSecret": "contract-not-a-real-secret"}

# One instance per role, so that an operation under test cannot change what another one
# needs, in whatever order they run. Credentials belong to an instance and an owner (the
# user or the agent): `authenticated` has some to update, `revocable` some to remove,
# `reauthenticable` some to clear, and `unauthenticated` has none.
WITH_CREDENTIALS = ("authenticated", "revocable", "reauthenticable")
CREDENTIAL_ROLES = (*WITH_CREDENTIALS, "unauthenticated")
INSTANCE_ROLES = ("readonly", "mutable", "disposable", "legacy", *CREDENTIAL_ROLES)
OAUTH_CONFIG_ROLES = ("readonly", "mutable", "disposable")
_FOR_AGENT = "agent"


def _create_instance(
    client: SessionClient, role: str, auth_type: str, **fields: Any
) -> dict[str, Any]:
    what = f"Create toolset instance ({role})"
    resp = client.request(
        "POST",
        _INSTANCES,
        json={
            "instanceName": f"contract-{role}-{uuid4().hex[:8]}",
            "toolsetType": _TOOLSET_TYPE,
            "authType": auth_type,
            **fields,
        },
    )
    instance = response_body(resp, (200, 201), what).get("instance") or {}
    assert instance.get("_id"), f"{what}: response has no instance._id"
    return instance


def _delete_instance(client: SessionClient, instance_id: str) -> requests.Response:
    return client.request("DELETE", f"{_INSTANCES}/{instance_id}")


def _delete_instances(client: SessionClient, instances: dict[str, str]) -> None:
    """Deleting an instance also deletes the credentials that users and agents have on it."""
    for role, instance_id in instances.items():
        delete_quietly(
            f"toolset instance ({role})",
            lambda instance_id=instance_id: _delete_instance(client, instance_id),
        )


def _api_token_instances(
    client: SessionClient, roles: tuple[str, ...], label: str, owner_path: str
) -> Iterator[dict[str, str]]:
    """One API-token instance per role. The owner at `owner_path` (the user, or an agent)
    gets credentials on those of `WITH_CREDENTIALS`."""
    created: dict[str, str] = {}
    try:
        for role in roles:
            created[role] = str(_create_instance(client, f"{label}{role}", _API_TOKEN)["_id"])
        for role in WITH_CREDENTIALS:
            response_body(
                client.request(
                    "POST",
                    f"{owner_path}/instances/{created[role]}/authenticate",
                    json=_CREDENTIALS,
                ),
                (200,),
                f"Authenticate on the toolset instance ({label}{role})",
            )
        yield created
    finally:
        _delete_instances(client, created)


@pytest.fixture(scope="module")
def contract_toolset_instances(user_session_client: SessionClient) -> Iterator[dict[str, str]]:
    yield from _api_token_instances(user_session_client, INSTANCE_ROLES, "", _TOOLSETS)


@pytest.fixture(scope="module")
def contract_service_account_agent(user_session_client: SessionClient) -> Iterator[str]:
    agents = AgentsClient(user_session_client)
    # No models: nothing asks the agent anything, and so it needs no configured LLM.
    resp = agents.create_agent(
        name=f"contract-service-account-{uuid4().hex[:8]}", isServiceAccount=True
    )
    agent = response_body(resp, (200, 201), "Create service-account agent").get("agent") or {}
    assert agent.get("_key"), "Create service-account agent: response has no agent._key"
    try:
        yield str(agent["_key"])
    finally:
        delete_quietly("service-account agent", lambda: agents.delete_agent(agent["_key"]))


@pytest.fixture(scope="module")
def contract_agent_toolset_instances(
    user_session_client: SessionClient, contract_service_account_agent: str
) -> Iterator[dict[str, str]]:
    yield from _api_token_instances(
        user_session_client,
        CREDENTIAL_ROLES,
        "agent-",
        f"{_TOOLSETS}/agents/{contract_service_account_agent}",
    )


@pytest.fixture(scope="module")
def contract_oauth_toolsets(
    user_session_client: SessionClient,
) -> Iterator[dict[str, dict[str, str]]]:
    client = user_session_client
    instances: dict[str, str] = {}
    configs: dict[str, str] = {}
    try:
        for role in OAUTH_CONFIG_ROLES:
            # The API creates an OAuth configuration only together with an instance.
            instance = _create_instance(client, f"oauth-{role}", _OAUTH, authConfig=_OAUTH_CLIENT)
            instances[role] = str(instance["_id"])
            assert instance.get("oauthConfigId"), (
                f"Create OAuth toolset instance ({role}): the API made no OAuth configuration"
            )
            configs[role] = str(instance["oauthConfigId"])
        for_agent = _create_instance(
            client, "oauth-agent", _OAUTH, oauthConfigId=configs["readonly"]
        )
        instances[_FOR_AGENT] = str(for_agent["_id"])
        # The API refuses to delete an OAuth configuration that an instance uses.
        response_body(
            _delete_instance(client, instances["disposable"]),
            (200,),
            "Delete the instance of the disposable OAuth configuration",
        )
        yield {"instances": instances, "configs": configs}
    finally:
        _delete_instances(client, instances)
        for role, config_id in configs.items():
            delete_quietly(
                f"OAuth configuration ({role})",
                lambda config_id=config_id: client.request(
                    "DELETE", f"{_OAUTH_CONFIGS}/{config_id}"
                ),
            )


@pytest.fixture(scope="module")
def contract_new_instance_name() -> str:
    """The name of an instance that a test case creates; the API refuses a name twice.

    `{case}` becomes the number of the request (helper/contract/values.py).
    """
    return f"contract-created-{uuid4().hex[:8]}-{{case}}"


def _instance_ids(fixture: str, prefix: str, roles: tuple[str, ...], what: str) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.id" for role in roles),
        lambda instances: tuple(instances[role] for role in roles),
        what,
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    _instance_ids(
        "contract_toolset_instances",
        "toolsetInstance",
        INSTANCE_ROLES,
        "Eight Jira toolset instances with API-token authentication: to read, to update, to "
        "delete, one for the operations from before instances, and four for the credential "
        "operations of the test user, who has made-up credentials on three of them (to update, "
        "to remove, to clear) and none on the fourth.",
    ),
    ValueSource(
        "contract_oauth_toolsets",
        (
            "toolsetInstance.oauth.id",
            "toolsetInstance.oauthForAgent.id",
            "oauthConfig.mutable.id",
            "oauthConfig.disposable.id",
        ),
        lambda oauth: (
            oauth["instances"]["readonly"],
            oauth["instances"][_FOR_AGENT],
            oauth["configs"]["mutable"],
            oauth["configs"]["disposable"],
        ),
        "Three OAuth configurations of the Jira toolset with a made-up client ID and secret, and "
        "three OAuth instances: two on the first configuration, for the authorization URL of the "
        "user and of the agent, and one on the second, which is there to be updated; the third "
        "configuration has no instance and is there to be deleted.",
    ),
    ValueSource(
        "contract_new_instance_name",
        ("toolsetInstance.new.name",),
        lambda name: (name,),
        "A name that is different in each request, for the instances that the test cases "
        "create; the fixture itself creates nothing.",
    ),
    ValueSource(
        "contract_service_account_agent",
        ("agent.serviceAccount.key",),
        lambda agent_key: (agent_key,),
        "A service-account agent with no model, toolset or knowledge; the API keeps toolset "
        "credentials only for an agent of that kind.",
    ),
    _instance_ids(
        "contract_agent_toolset_instances",
        "agentToolsetInstance",
        CREDENTIAL_ROLES,
        "Four more Jira API-token instances, for the credential operations of the agent, which "
        "has made-up credentials on three of them (to update, to remove, to clear) and none on "
        "the fourth.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
