"""Fixtures for the auth contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`user.mutable.accessToken`, ...).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from helper.clients.auth_client import AuthClient, UserAccountClient
from helper.config import TEST_USER_PASSWORD
from helper.contract.pytest_support import response_body, restore_quietly, suite_fixtures
from helper.contract.sources import ValueSource
from helper.http.session_client import SessionClient
from helper.pipeshub_client import PipeshubClient
from helper.second_user import SecondUser, create_second_user, delete_second_user

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# `readonly` signs in and gets its token refreshed; a logout or a password change of its user
# would make the refresh token invalid. `mutable` is logged out and gets its password changed;
# both operations check only the signature of its session token, so neither can spoil the other.
READONLY = "readonly"
MUTABLE = "mutable"
USER_ROLES = (READONLY, MUTABLE)

# The sign-in routes count 10 requests per minute and client IP by default
# (MAX_AUTH_REQUESTS_PER_MINUTE), together with the sign-in operations under test.
_SIGN_IN_LIMITER_WINDOW_SEC = 60

_SAML_CONFIGURATION = "/api/v1/configurationManager/authConfig/sso"
# What the configuration-manager suite also leaves where there was no SAML configuration: the
# API can set one and cannot remove it. `.invalid` never resolves (RFC 2606).
_SAML_PLACEHOLDER = {
    "entryPoint": "http://contract-test-unset.invalid/sso",
    "certificate": "contract-test-unset",
    "emailKey": "email",
    "enableJit": False,
}


def _let_the_sign_in_limiter_reset() -> None:
    time.sleep(_SIGN_IN_LIMITER_WINDOW_SEC + 1)


def _sign_in_session(account: UserAccountClient, email: str) -> str:
    started = account.init_auth(email)
    session_token = started.headers.get("x-session-token")
    assert started.status_code == 200 and session_token, (
        f"Start sign-in ({email}): HTTP {started.status_code} {started.text[:300]}"
    )
    return str(session_token)


def _refresh_token(account: UserAccountClient, email: str) -> str:
    """Sign in once more: `create_second_user` keeps only the session token of its sign-in."""
    signed_in = response_body(
        account.authenticate(_sign_in_session(account, email), email, TEST_USER_PASSWORD),
        (200,),
        f"Sign in ({email})",
    )
    assert signed_in.get("refreshToken"), f"Sign in ({email}): response has no refreshToken"
    return str(signed_in["refreshToken"])


@pytest.fixture(scope="module")
def contract_users(
    pipeshub_client: PipeshubClient, user_account_client: UserAccountClient
) -> Iterator[dict[str, dict[str, str]]]:
    pipeshub_client._ensure_access_token()
    users: dict[str, SecondUser] = {}
    try:
        # The seven sign-in requests below need room in the limiter, and the run needs it after.
        _let_the_sign_in_limiter_reset()
        for role in USER_ROLES:
            users[role] = create_second_user(pipeshub_client)
        readonly_email = users[READONLY].email
        refresh_token = _refresh_token(user_account_client, readonly_email)
        sign_in_session = _sign_in_session(user_account_client, readonly_email)
        _let_the_sign_in_limiter_reset()
        yield {
            READONLY: {
                "refreshToken": refresh_token,
                "password": TEST_USER_PASSWORD,
                "signInSession": sign_in_session,
            },
            MUTABLE: {
                "accessToken": users[MUTABLE].token,
                "password": TEST_USER_PASSWORD,
                "newPassword": f"Contract-{uuid4().hex[:8]}-aA1!",
            },
        }
    finally:
        for user in users.values():
            delete_second_user(pipeshub_client, user)


def _policy(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "order": step["order"],
            "allowedMethods": [{"type": method["type"]} for method in step["allowedMethods"]],
        }
        for step in steps
    ]


@pytest.fixture(scope="module")
def contract_sign_in_policy(user_session_client: SessionClient) -> Iterator[str]:
    """The sign-in policy as it was before the run; it is put back after it.

    The session that puts the policy back starts here, before the run, and the route checks
    only its signature.
    """
    policy = AuthClient(user_session_client)
    steps = response_body(policy.get_auth_methods(), (200,), "Read the sign-in policy").get(
        "authMethods"
    )
    assert steps, "Read the sign-in policy: response has no authMethods"
    saved = {"authMethod": _policy(steps)}
    try:
        yield json.dumps(saved)
    finally:
        put_back = restore_quietly("sign-in policy", lambda: policy.update_auth_method(json=saved))
        assert put_back, (
            "The contract run changed the sign-in policy of the organization and could not put "
            "it back. Send this to POST /api/v1/orgAuthConfig/updateAuthMethod by hand: "
            f"{json.dumps(saved)}"
        )


@pytest.fixture(scope="module")
def contract_saml_configuration(pipeshub_client: PipeshubClient) -> str:
    """`GET /saml/signIn` redirects only when a SAML identity provider is configured."""
    configuration = response_body(
        pipeshub_client.request("GET", _SAML_CONFIGURATION), (200,), "Read the SAML configuration"
    )
    if configuration.get("entryPoint"):
        return "set before the run"
    response_body(
        pipeshub_client.request("POST", _SAML_CONFIGURATION, json=_SAML_PLACEHOLDER),
        (200,),
        "Set a placeholder SAML configuration",
    )
    return "placeholder, set by this fixture"


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# `contract_users` is last: it waits for the sign-in limiter after every sign-in of the fixtures.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_saml_configuration",
        ("saml.configuration",),
        lambda state: (state,),
        "The SAML sign-in configuration. Where the deployment has none, the fixture writes one "
        "(a placeholder identity provider under `.invalid`). The API cannot remove a SAML "
        "configuration, so the placeholder stays on the deployment after the run. It also "
        "stays if the API answers 500 because it cannot use the placeholder certificate: the "
        "API stores the configuration first.",
    ),
    ValueSource(
        "contract_sign_in_policy",
        ("signInPolicy.saved",),
        lambda saved: (saved,),
        "The sign-in policy of the organization as it was before the run. The fixture writes "
        "it back when the tests of this suite are done.",
    ),
    ValueSource(
        "contract_users",
        (
            "user.readonly.refreshToken",
            "user.readonly.password",
            "user.readonly.signInSession",
            "user.mutable.accessToken",
            "user.mutable.password",
            "user.mutable.newPassword",
        ),
        lambda users: (
            users[READONLY]["refreshToken"],
            users[READONLY]["password"],
            users[READONLY]["signInSession"],
            users[MUTABLE]["accessToken"],
            users[MUTABLE]["password"],
            users[MUTABLE]["newPassword"],
        ),
        "Two users who sign in with a password, made with `create_second_user`. Of one: its "
        "refresh token, its password and a sign-in session that `initAuth` started for it. Of "
        "the other, which is logged out and gets its password changed: its session token, its "
        "password and the new password.",
        secret=True,
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
