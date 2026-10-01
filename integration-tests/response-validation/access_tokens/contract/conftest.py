"""Fixtures for the access-token contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`personalAccessToken.disposable.id`, `serviceAccount.mutable.id`, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.contract.values import CASE_NUMBER
from helper.http.session_client import SessionClient
from helper.pipeshub_client import PipeshubClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# One object per role, so that an update, a revocation or a delete under test cannot change
# what another operation reads, in whatever order they run.
READONLY = "readonly"
# The owner revokes `disposable`; the admin operation revokes `adminDisposable`.
REVOCABLE_ROLES = ("disposable", "adminDisposable")
SERVICE_ACCOUNT_ROLES = (READONLY, "mutable", "disposable")
SERVICE_TOKEN_ROLES = (READONLY, "disposable")
# The plan sends 19 requests to each of the two revoke operations. Each request takes the next
# token of a pool, so none of them meets a token that an earlier request revoked.
REVOKE_REQUESTS = 19

_PERSONAL_ACCESS_TOKENS = "/api/v1/personal-access-tokens"
_SERVICE_ACCOUNTS = "/api/v1/service-accounts"
_SERVICE_TOKENS = "/api/v1/service-tokens"
# The slugs are the same in every run, and only this suite uses the prefix. A delete only
# marks a service account, and a create with the slug of a deleted one brings that record
# back. So every run uses the same records again; a new slug in each run would leave one
# more record each time, with its graph node and its knowledge base.
SLUG_PREFIX = "contract-access-tokens-"
# The scope that lets a token do the least.
_PREFERRED_SCOPE = "openid"


def _name(role: str) -> str:
    return f"contract-{role}-{uuid4().hex[:8]}"


def _scope(resp: requests.Response, what: str) -> str:
    """One scope that the deployment allows for a token: `openid` if it does, else the first."""
    names = [
        str(scope["name"])
        for scope in response_body(resp, (200,), what).get("scopes") or []
        if isinstance(scope, dict) and scope.get("name")
    ]
    if not names:
        pytest.skip(f"{what}: the deployment allows no scope for a token (MCP_SCOPES)")
    return _PREFERRED_SCOPE if _PREFERRED_SCOPE in names else names[0]


@pytest.fixture(scope="module")
def contract_personal_access_token_scope(user_session_client: SessionClient) -> str:
    return _scope(
        user_session_client.request("GET", f"{_PERSONAL_ACCESS_TOKENS}/scopes"),
        "List the scopes of personal access tokens",
    )


@pytest.fixture(scope="module")
def contract_service_token_scope(pipeshub_client: PipeshubClient) -> str:
    return _scope(
        pipeshub_client.request("GET", f"{_SERVICE_TOKENS}/scopes"),
        "List the scopes of service tokens",
    )


@pytest.fixture(scope="module")
def contract_personal_access_tokens(
    user_session_client: SessionClient, contract_personal_access_token_scope: str
) -> Iterator[dict[str, list[str]]]:
    def _create(role: str) -> str:
        resp = user_session_client.request(
            "POST",
            _PERSONAL_ACCESS_TOKENS,
            json={
                "name": _name(role),
                "scopes": [contract_personal_access_token_scope],
                # The shortest time the API offers.
                "expiryDays": 30,
            },
        )
        token = response_body(resp, (201,), f"Create personal access token ({role})").get("token")
        assert isinstance(token, dict) and token.get("id"), (
            f"Create personal access token ({role}): response has no token.id"
        )
        return str(token["id"])

    def _revoke(token_id: str) -> requests.Response:
        return user_session_client.request("DELETE", f"{_PERSONAL_ACCESS_TOKENS}/{token_id}")

    created: dict[str, list[str]] = {}
    try:
        created.setdefault(READONLY, []).append(_create(READONLY))
        for role in REVOCABLE_ROLES:
            for _ in range(REVOKE_REQUESTS):
                created.setdefault(role, []).append(_create(role))
        yield created
    finally:
        for role, token_ids in created.items():
            for token_id in token_ids:
                delete_quietly(f"personal access token ({role})", partial(_revoke, token_id))


def _delete_service_account(client: PipeshubClient, account_id: str) -> requests.Response:
    return client.request("DELETE", f"{_SERVICE_ACCOUNTS}/{account_id}")


@pytest.fixture(scope="module")
def contract_swept_service_accounts(pipeshub_client: PipeshubClient) -> None:
    """Deletes the service accounts that a run of this suite left live when it stopped early.

    Their slugs are the ones this run uses, and a create would be answered with 409.
    """
    listed = response_body(
        pipeshub_client.request("GET", _SERVICE_ACCOUNTS), (200,), "List service accounts"
    )
    for account in listed.get("serviceAccounts") or []:
        if str(account.get("slug") or "").startswith(SLUG_PREFIX) and account.get("id"):
            delete_quietly(
                f"service account `{account['slug']}` of an earlier run",
                partial(_delete_service_account, pipeshub_client, str(account["id"])),
            )


@pytest.fixture(scope="module")
def contract_service_accounts(
    pipeshub_client: PipeshubClient, contract_swept_service_accounts: None
) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in SERVICE_ACCOUNT_ROLES:
            slug = f"{SLUG_PREFIX}{role}"
            resp = pipeshub_client.request(
                "POST", _SERVICE_ACCOUNTS, json={"slug": slug, "fullName": slug}
            )
            account_id = response_body(resp, (201,), f"Create service account ({role})").get("id")
            assert account_id, f"Create service account ({role}): response has no id"
            created[role] = str(account_id)
        yield created
    finally:
        for role, account_id in created.items():
            delete_quietly(
                f"service account ({role})",
                partial(_delete_service_account, pipeshub_client, account_id),
            )


@pytest.fixture(scope="module")
def contract_service_account_slug(contract_swept_service_accounts: None) -> str:
    """The slug for the accounts that the test cases create; `{case}` is the request number."""
    return f"{SLUG_PREFIX}created-{CASE_NUMBER}"


@pytest.fixture(scope="module")
def contract_service_tokens(
    pipeshub_client: PipeshubClient,
    contract_service_accounts: dict[str, str],
    contract_service_token_scope: str,
) -> Iterator[dict[str, str]]:
    account_id = contract_service_accounts[READONLY]
    created: dict[str, str] = {}
    try:
        for role in SERVICE_TOKEN_ROLES:
            resp = pipeshub_client.request(
                "POST",
                _SERVICE_TOKENS,
                json={
                    "serviceAccountId": account_id,
                    "name": _name(role),
                    "scopes": [contract_service_token_scope],
                    "expiryDays": 1,
                },
            )
            token = response_body(resp, (201,), f"Create service token ({role})").get("token")
            assert isinstance(token, dict) and token.get("id"), (
                f"Create service token ({role}): response has no token.id"
            )
            created[role] = str(token["id"])
        yield created
    finally:
        for role, token_id in created.items():
            delete_quietly(
                f"service token ({role})",
                lambda token_id=token_id: pipeshub_client.request(
                    "DELETE",
                    f"{_SERVICE_TOKENS}/{token_id}",
                    params={"serviceAccountId": account_id},
                ),
            )


def _by_role(fixture: str, prefix: str, roles: tuple[str, ...], what: str) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.id" for role in roles),
        lambda by_role: tuple(by_role[role] for role in roles),
        what,
    )


def _token_values(tokens: dict[str, list[str]]) -> tuple[Any, ...]:
    """The token to read, and a pool of tokens for each revoke operation."""
    return (tokens[READONLY][0], *(tokens[role] for role in REVOCABLE_ROLES))


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# No value is a credential: a fixture keeps only the ID of a token, never the token itself.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_personal_access_token_scope",
        ("scope.personalAccessToken.name",),
        lambda scope: (scope,),
        "A scope that the deployment allows for a personal access token, read from "
        "`GET /personal-access-tokens/scopes`. The fixture creates nothing.",
    ),
    ValueSource(
        "contract_service_token_scope",
        ("scope.serviceToken.name",),
        lambda scope: (scope,),
        "A scope that the deployment allows for a service token, read from "
        "`GET /service-tokens/scopes`. The fixture creates nothing.",
    ),
    ValueSource(
        "contract_personal_access_tokens",
        tuple(f"personalAccessToken.{role}.id" for role in (READONLY, *REVOCABLE_ROLES)),
        _token_values,
        f"{1 + len(REVOCABLE_ROLES) * REVOKE_REQUESTS} personal access tokens of the test user: "
        f"one that stays in the lists, and {REVOKE_REQUESTS} for each of the two revoke "
        "operations, one for each of its requests. The teardown revokes them.",
    ),
    _by_role(
        "contract_service_accounts",
        "serviceAccount",
        SERVICE_ACCOUNT_ROLES,
        "Three service accounts: to read (it also holds the service tokens), to update, and "
        f"to delete. Their slugs are the same in every run (`{SLUG_PREFIX}readonly`, ...), so "
        "every run uses the same three records again. First, the accounts that an earlier run "
        "left live are deleted.",
    ),
    _by_role(
        "contract_service_tokens",
        "serviceToken",
        SERVICE_TOKEN_ROLES,
        "Two service tokens of the service account to read: one that stays in the list and "
        "one to revoke.",
    ),
    ValueSource(
        "contract_service_account_slug",
        ("serviceAccount.created.slug",),
        lambda slug: (slug,),
        f"The slug of the service accounts that the test cases create, `{SLUG_PREFIX}created-` "
        "and the number of the request. The numbers are the same in every run, so the runs "
        "use the same records again. The fixture itself creates nothing.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
