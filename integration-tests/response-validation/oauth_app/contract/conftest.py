"""Fixtures for the OAuth apps contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`app.mutable.id`, `serviceAccount.identity.id`, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from helper.clients.oauth_client import OAuthAppsClient, OAuthProviderClient
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.http.session_client import SessionClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# One app per role, so that a suspension, a new secret, a revocation or a delete under test
# cannot change what another operation reads, in whatever order they run.
# `suspendable` is there to be suspended; `suspended` already is, to be activated.
SUSPENDED = "suspended"
APP_ROLES = (
    "readonly",
    "mutable",
    "suspendable",
    SUSPENDED,
    "rotatable",
    "revocable",
    "disposable",
)
# The apps that hold an access token: one for the token list, one for the revocation.
TOKEN_ROLES = ("readonly", "revocable")
_SERVICE_ACCOUNTS = "/api/v1/service-accounts"
# The same slug in every run, and one that only this suite uses. A delete only marks a
# service account, and a create with the slug of a deleted one brings that record back.
# So every run uses one record again; a new slug in each run would leave one more each time.
IDENTITY_SLUG = "contract-oauth-apps-identity"


def _name(role: str) -> str:
    return f"contract-{role}-{uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def contract_oauth_apps(oauth_apps_client: OAuthAppsClient) -> Iterator[dict[str, dict[str, str]]]:
    created: dict[str, dict[str, str]] = {}
    try:
        for role in APP_ROLES:
            # `openid` alone leaves a client-credentials token without a scope:
            # the token endpoint removes the identity scopes from it.
            resp = oauth_apps_client.create_app(
                name=_name(role),
                allowedGrantTypes=["client_credentials"],
                allowedScopes=["openid", "kb:read"],
            )
            app = response_body(resp, (201,), f"Create OAuth app ({role})").get("app") or {}
            missing = [key for key in ("id", "clientId", "clientSecret") if not app.get(key)]
            assert not missing, f"Create OAuth app ({role}): response has no app.{missing[0]}"
            created[role] = {key: str(app[key]) for key in ("id", "clientId", "clientSecret")}
        response_body(
            oauth_apps_client.suspend_app(created[SUSPENDED]["id"]), (200,), "Suspend OAuth app"
        )
        yield created
    finally:
        for role, app in created.items():
            delete_quietly(
                f"OAuth app ({role})", lambda app=app: oauth_apps_client.delete_app(app["id"])
            )


@pytest.fixture(scope="module")
def contract_oauth_app_tokens(
    contract_oauth_apps: dict[str, dict[str, str]],
    oauth_apps_client: OAuthAppsClient,
    oauth_provider_client: OAuthProviderClient,
) -> dict[str, str]:
    """The ID of an access token of each app in `TOKEN_ROLES`. Deleting the app revokes it."""
    token_ids: dict[str, str] = {}
    for role in TOKEN_ROLES:
        app = contract_oauth_apps[role]
        response_body(
            oauth_provider_client.token(
                grant_type="client_credentials",
                client_id=app["clientId"],
                client_secret=app["clientSecret"],
            ),
            (200,),
            f"Issue a token to the OAuth app ({role})",
        )
        listed = response_body(
            oauth_apps_client.list_tokens(app["id"]), (200,), f"List tokens of the app ({role})"
        )
        tokens = listed.get("tokens") or []
        assert tokens and tokens[0].get("id"), f"List tokens of the app ({role}): no token"
        token_ids[role] = str(tokens[0]["id"])
    return token_ids


@pytest.fixture(scope="module")
def contract_service_account(user_session_client: SessionClient) -> Iterator[str]:
    def _delete(account_id: str) -> requests.Response:
        return user_session_client.request("DELETE", f"{_SERVICE_ACCOUNTS}/{account_id}")

    listed = response_body(
        user_session_client.request("GET", _SERVICE_ACCOUNTS), (200,), "List service accounts"
    )
    # A run that stopped early left its account live, and a create would be answered with 409.
    for account in listed.get("serviceAccounts") or []:
        if account.get("slug") == IDENTITY_SLUG and account.get("id"):
            delete_quietly(
                "service account of an earlier run", partial(_delete, str(account["id"]))
            )
    resp = user_session_client.request(
        "POST", _SERVICE_ACCOUNTS, json={"slug": IDENTITY_SLUG, "fullName": IDENTITY_SLUG}
    )
    account_id = response_body(resp, (201,), "Create service account").get("id")
    assert account_id, "Create service account: response has no id"
    try:
        yield str(account_id)
    finally:
        delete_quietly("service account", partial(_delete, str(account_id)))


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_oauth_apps",
        tuple(f"app.{role}.id" for role in APP_ROLES),
        lambda apps: tuple(apps[role]["id"] for role in APP_ROLES),
        "Seven OAuth apps of the test user: to read, to update, to suspend, already suspended "
        "(to activate), to get a new secret, to lose its tokens, and to delete. The teardown "
        "deletes them, which only marks them as deleted: their seven records stay.",
    ),
    ValueSource(
        "contract_oauth_app_tokens",
        tuple(f"app.{role}.tokenId" for role in TOKEN_ROLES),
        lambda token_ids: tuple(token_ids[role] for role in TOKEN_ROLES),
        "One access token for the app to read and one for the app that loses its tokens, each "
        "issued with the client credentials of its app.",
    ),
    ValueSource(
        "contract_service_account",
        ("serviceAccount.identity.id",),
        lambda account_id: (account_id,),
        "One service account, to point the tokens of an app at. It has the same slug in every "
        f"run (`{IDENTITY_SLUG}`), so every run uses the same record again.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
