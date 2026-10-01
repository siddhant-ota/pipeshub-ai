"""Fixtures for the OAuth provider contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`oauthApp.clientId`, `oauthToken.readonly.accessToken`, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import requests

from helper.clients.oauth_client import OAuthAppsClient, OAuthProviderClient
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.contract.suite import load_suite
from helper.http.session_client import SessionClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# `openid` lets the user token call userinfo, and is the scope that suite.yaml puts into the
# requests (`oauthApp.scope`); the other two put the name and email claims into the userinfo
# answer. None of them gives access to any data of the organization.
APP_SCOPES = ("openid", "profile", "email")


def _access_token(resp: requests.Response, what: str) -> str:
    token = response_body(resp, (200,), what).get("access_token")
    assert token, f"{what}: response has no access_token"
    return str(token)


@pytest.fixture(scope="module")
def contract_oauth_app(
    oauth_apps_client: OAuthAppsClient,
    oauth_provider_client: OAuthProviderClient,
    user_session_client: SessionClient,
) -> Iterator[dict[str, str]]:
    """An OAuth app with a token of each kind: one a user granted, one of the app itself."""
    redirect_uri = load_suite(SUITE_PATH).constants["oauthApp.redirectUri"]
    resp = oauth_apps_client.create_app(
        name=f"contract-readonly-{uuid4().hex[:8]}",
        redirectUris=[redirect_uri],
        allowedGrantTypes=["authorization_code", "client_credentials"],
        allowedScopes=list(APP_SCOPES),
    )
    app = response_body(resp, (201,), "Create OAuth app").get("app") or {}
    assert app.get("id"), "Create OAuth app: response has no app.id"
    app_id = str(app["id"])
    try:
        assert app.get("clientId") and app.get("clientSecret"), (
            "Create OAuth app: response has no app.clientId or app.clientSecret"
        )
        client = {"client_id": str(app["clientId"]), "client_secret": str(app["clientSecret"])}

        # Consent is the test user's own act: only its session may give it.
        consent = response_body(
            OAuthProviderClient(user_session_client).authorize_consent(
                client_id=client["client_id"],
                redirect_uri=redirect_uri,
                scope=" ".join(APP_SCOPES),
                state=f"contract-{uuid4().hex[:8]}",
                consent="granted",
                auth=True,
            ),
            (200,),
            "Consent to the OAuth app",
        )
        code = parse_qs(urlsplit(str(consent.get("redirectUrl") or "")).query).get("code")
        assert code, "Consent to the OAuth app: the redirect URL has no code"
        user_token = _access_token(
            oauth_provider_client.token(
                grant_type="authorization_code", code=code[0], redirect_uri=redirect_uri, **client
            ),
            "Exchange the authorization code",
        )
        app_token = _access_token(
            oauth_provider_client.token(grant_type="client_credentials", **client),
            "Client-credentials token of the OAuth app",
        )
        yield {
            "clientId": client["client_id"],
            "clientSecret": client["client_secret"],
            "userToken": user_token,
            "appToken": app_token,
        }
    finally:
        # Deleting the app leaves the tokens that users granted it valid until they expire.
        delete_quietly(
            "tokens of the OAuth app", lambda: oauth_apps_client.revoke_all_tokens(app_id)
        )
        delete_quietly("OAuth app", lambda: oauth_apps_client.delete_app(app_id))


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_oauth_app",
        ("oauthApp.clientId",),
        lambda app: (app["clientId"],),
        "An OAuth app that the fixture registers for this suite, with the grants "
        "`authorization_code` and `client_credentials` and the scopes `openid`, `profile` and "
        "`email`. It is not the client that the tests log in with.",
    ),
    ValueSource(
        "contract_oauth_app",
        (
            "oauthApp.clientSecret",
            "oauthToken.readonly.accessToken",
            "oauthToken.disposable.accessToken",
        ),
        lambda app: (app["clientSecret"], app["userToken"], app["appToken"]),
        "The client secret of that app and two access tokens of it: one that the test user "
        "granted it (consent, then the code exchange), which is only read, and one "
        "client-credentials token, which the revoke operation revokes.",
        secret=True,
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
