"""Fixtures for the access-token contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`personalAccessToken.disposable.id`, `serviceAccount.mutable.id`, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

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
PERSONAL_ACCESS_TOKEN_ROLES = (READONLY, "disposable", "adminDisposable")
SERVICE_ACCOUNT_ROLES = (READONLY, "mutable", "disposable")
SERVICE_TOKEN_ROLES = (READONLY, "disposable")

_PERSONAL_ACCESS_TOKENS = "/api/v1/personal-access-tokens"
_SERVICE_ACCOUNTS = "/api/v1/service-accounts"
_SERVICE_TOKENS = "/api/v1/service-tokens"
# The fixture tokens can do as little as a token can, and for as short a time. `openid` is
# also the scope that suite.yaml gives the generated requests.
_SCOPES = ["openid"]


def _name(role: str) -> str:
    return f"contract-{role}-{uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def contract_personal_access_tokens(user_session_client: SessionClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in PERSONAL_ACCESS_TOKEN_ROLES:
            resp = user_session_client.request(
                "POST",
                _PERSONAL_ACCESS_TOKENS,
                json={"name": _name(role), "scopes": _SCOPES, "expiryDays": 30},
            )
            token = response_body(resp, (201,), f"Create personal access token ({role})").get(
                "token"
            )
            assert isinstance(token, dict) and token.get("id"), (
                f"Create personal access token ({role}): response has no token.id"
            )
            created[role] = str(token["id"])
        yield created
    finally:
        for role, token_id in created.items():
            delete_quietly(
                f"personal access token ({role})",
                lambda token_id=token_id: user_session_client.request(
                    "DELETE", f"{_PERSONAL_ACCESS_TOKENS}/{token_id}"
                ),
            )


@pytest.fixture(scope="module")
def contract_service_accounts(pipeshub_client: PipeshubClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in SERVICE_ACCOUNT_ROLES:
            name = _name(role)
            resp = pipeshub_client.request(
                "POST", _SERVICE_ACCOUNTS, json={"slug": name, "fullName": name}
            )
            account_id = response_body(resp, (201,), f"Create service account ({role})").get("id")
            assert account_id, f"Create service account ({role}): response has no id"
            created[role] = str(account_id)
        yield created
    finally:
        for role, account_id in created.items():
            delete_quietly(
                f"service account ({role})",
                lambda account_id=account_id: pipeshub_client.request(
                    "DELETE", f"{_SERVICE_ACCOUNTS}/{account_id}"
                ),
            )


@pytest.fixture(scope="module")
def contract_service_tokens(
    pipeshub_client: PipeshubClient, contract_service_accounts: dict[str, str]
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
                    "scopes": _SCOPES,
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


@pytest.fixture(scope="module")
def contract_service_account_slug() -> str:
    """A slug that no earlier run used; `{case}` makes it another one in each request."""
    return f"contract-{uuid4().hex[:8]}-{CASE_NUMBER}"


def _by_role(fixture: str, prefix: str, roles: tuple[str, ...], what: str) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.id" for role in roles),
        lambda by_role: tuple(by_role[role] for role in roles),
        what,
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# No value is a credential: a fixture keeps only the ID of a token, never the token itself.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    _by_role(
        "contract_personal_access_tokens",
        "personalAccessToken",
        PERSONAL_ACCESS_TOKEN_ROLES,
        "Three personal access tokens of the test user: one that stays in the lists, one for "
        "its owner to revoke, and one for the admin operation to revoke.",
    ),
    _by_role(
        "contract_service_accounts",
        "serviceAccount",
        SERVICE_ACCOUNT_ROLES,
        "Three service accounts: to read (it also holds the service tokens), to update, and "
        "to delete.",
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
        "The slug of the service accounts that the test cases create: new in each run and, "
        "with its `{case}` number, in each request. The fixture itself creates nothing.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
