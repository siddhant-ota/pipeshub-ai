"""Fixtures for the configuration-manager contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`slackBot.mutable.id`, `smtp.saved`, ...).

Most writing operations of this suite change a setting of the whole
organization. Each such setting has a fixture that reads it before the run
(`<setting>.saved`) and puts it back in its teardown, when the tests of the
suite are done.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from helper.contract.pytest_support import (
    delete_quietly,
    response_body,
    restore_quietly,
    suite_fixtures,
)
from helper.contract.sources import ValueSource
from helper.http.session_client import SessionClient
from helper.pipeshub_client import PipeshubClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

_BASE = "/api/v1/configurationManager"

# One object per role, so that an update or a delete under test cannot change what another
# operation uses, in whatever order they run.
ROLES = ("mutable", "disposable")
# `promotable` is made the default provider by the operation that sets the default.
PROVIDER_ROLES = (*ROLES, "promotable")
AUTH_PROVIDERS = ("azureAd", "microsoft", "google", "sso", "oauth")

# What a `<setting>.saved` value says in the report. The setting itself can be a secret.
_SET = "set"
_NOT_SET = "not set"
# What the API answers in place of a secret when the deployment runs with HIDE_SECRET_CONFIG
# (maskConfigSecrets.ts). Written back, it would replace the secret.
_MASK = "****************"

# What is left where a setting had no value before the run and the API has no way to remove
# it again. `.invalid` never resolves (RFC 2606).
_PLACEHOLDER = "contract-test-unset"
_PLACEHOLDER_URL = "http://contract-test-unset.invalid"
_MICROSOFT_FIELDS = ("clientId", "tenantId", "enableJit")
_MICROSOFT_PLACEHOLDER = {"clientId": _PLACEHOLDER, "tenantId": "common", "enableJit": False}
# provider -> (the fields its setter takes, what it gets if it had no configuration)
_AUTH_CONFIGS: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {
    "azureAd": (_MICROSOFT_FIELDS, _MICROSOFT_PLACEHOLDER),
    "microsoft": (_MICROSOFT_FIELDS, _MICROSOFT_PLACEHOLDER),
    "google": (("clientId", "enableJit"), {"clientId": _PLACEHOLDER, "enableJit": False}),
    "sso": (
        ("entryPoint", "certificate", "emailKey", "samlPlatform", "enableJit"),
        {
            "entryPoint": f"{_PLACEHOLDER_URL}/sso",
            "certificate": _PLACEHOLDER,
            "emailKey": "email",
            "enableJit": False,
        },
    ),
    "oauth": (
        (
            "providerName",
            "clientId",
            "clientSecret",
            "authorizationUrl",
            "tokenEndpoint",
            "userInfoEndpoint",
            "scope",
            "redirectUri",
            "enableJit",
        ),
        {"providerName": _PLACEHOLDER, "clientId": _PLACEHOLDER, "enableJit": False},
    ),
}


def _state(before: Any) -> str:
    return _SET if before else _NOT_SET


def _has(read: Callable[[], Any], value: Any) -> bool:
    try:
        return read() == value
    except Exception:  # noqa: BLE001 - a setting that cannot be read does not have the value
        return False


@contextmanager
def _saved(
    what: str,
    read: Callable[[], Any],
    write: Callable[[Any], object],
    *,
    if_unset: Any = None,
) -> Iterator[Any]:
    """What `read` gives now; on exit, `write` puts it back if the run changed the setting.

    `read` gives something falsy for a setting that has no value. The API can
    remove none of these settings. One that had no value gets `if_unset` after
    the run; with no `if_unset`, the fixture is skipped and the operation that
    changes the setting is not sent.
    """
    before = read()
    if isinstance(before, dict) and _MASK in before.values():
        pytest.skip(
            f"The API masks {what} (HIDE_SECRET_CONFIG), so it cannot be read to put it back."
        )
    if not before and if_unset is None:
        pytest.skip(
            f"The deployment has no {what}, and the API cannot remove one: "
            "the run would leave a generated one behind."
        )
    try:
        yield before
    finally:
        after = before or if_unset
        if not _has(read, before):
            write(after)
            # No value in the message: the setting can be a secret.
            assert _has(read, after), (
                f"The contract run changed {what} and could not put it back. Set it again by hand."
            )


def _get(client: PipeshubClient, path: str, what: str) -> dict[str, Any]:
    return response_body(client.request("GET", f"{_BASE}{path}"), (200,), f"Read {what}")


def _restore(client: PipeshubClient, method: str, path: str, what: str, body: Any = None) -> bool:
    return restore_quietly(what, lambda: client.request(method, f"{_BASE}{path}", json=body))


def _saved_fields(
    client: PipeshubClient,
    what: str,
    path: str,
    fields: tuple[str, ...],
    *,
    method: str = "POST",
    if_unset: dict[str, Any] | None = None,
) -> Any:
    """Save a setting that one GET returns and one request with the same fields writes."""

    def read() -> dict[str, Any]:
        body = _get(client, path, what)
        return {name: body[name] for name in fields if name in body}

    def write(value: dict[str, Any]) -> bool:
        return _restore(client, method, path, what, value)

    return _saved(what, read, write, if_unset=if_unset)


@pytest.fixture(scope="module")
def contract_smtp(pipeshub_client: PipeshubClient, smtp_ready: str) -> Iterator[Any]:
    # The API cannot remove an SMTP configuration, so the run needs one to put back.
    # `smtp_ready` writes one from the SMTP_* environment if the deployment has none,
    # and skips without that environment.
    del smtp_ready
    fields = ("host", "port", "username", "password", "fromEmail")
    with _saved_fields(pipeshub_client, "SMTP configuration", "/smtpConfig", fields) as before:
        yield before


@pytest.fixture(scope="module")
def contract_auth_configs(pipeshub_client: PipeshubClient) -> Iterator[dict[str, Any]]:
    def saved(provider: str) -> Any:
        fields, placeholder = _AUTH_CONFIGS[provider]
        path = f"/authConfig/{provider}"
        what = f"{provider} sign-in configuration"

        def read() -> dict[str, Any]:
            body = _get(pipeshub_client, path, what)
            config = {name: body[name] for name in fields if name in body}
            # To the sign-in code, no `enableJit` is off. The setter turns it on when it is
            # left out.
            return {**config, "enableJit": config.get("enableJit") is True} if config else {}

        def write(value: dict[str, Any]) -> bool:
            return _restore(pipeshub_client, "POST", path, what, value)

        return _saved(what, read, write, if_unset=placeholder)

    with ExitStack() as stack:
        yield {provider: stack.enter_context(saved(provider)) for provider in AUTH_PROVIDERS}


@pytest.fixture(scope="module")
def contract_frontend_public_url(pipeshub_client: PipeshubClient) -> Iterator[Any]:
    with _saved_fields(
        pipeshub_client, "frontend public URL", "/frontendPublicUrl", ("url",)
    ) as before:
        yield before


@pytest.fixture(scope="module")
def contract_connector_public_url(pipeshub_client: PipeshubClient) -> Iterator[Any]:
    with _saved_fields(
        pipeshub_client,
        "connector public URL",
        "/connectorPublicUrl",
        ("url",),
        if_unset={"url": _PLACEHOLDER_URL},
    ) as before:
        yield before


@pytest.fixture(scope="module")
def contract_platform_settings(pipeshub_client: PipeshubClient) -> Iterator[Any]:
    with _saved_fields(
        pipeshub_client,
        "platform settings",
        "/platform/settings",
        ("fileUploadMaxSizeBytes", "featureFlags"),
    ) as before:
        yield before


@pytest.fixture(scope="module")
def contract_system_prompts(pipeshub_client: PipeshubClient) -> Iterator[Any]:
    with _saved_fields(
        pipeshub_client,
        "custom system prompts",
        "/prompts/system",
        ("customSystemPrompt", "customSystemPromptWebSearch", "customSystemPromptAgent"),
        method="PUT",
    ) as before:
        yield before


@pytest.fixture(scope="module")
def contract_metrics_collection(pipeshub_client: PipeshubClient) -> Iterator[dict[str, Any]]:
    what = "metrics collection settings"

    def read() -> dict[str, Any]:
        # The setters store the switch and the interval as strings.
        config = _get(pipeshub_client, "/metricsCollection", what)
        return {
            "enableMetricCollection": str(config["enableMetricCollection"]).lower() == "true",
            "pushIntervalMs": float(config["pushIntervalMs"]),
            "serverUrl": str(config["serverUrl"]),
        }

    def write(value: dict[str, Any]) -> None:
        # Only what changed, and the switch last: a request that turns it off makes the
        # server post its metrics to the server URL at once, also when it is off already.
        current: dict[str, Any] = {}
        with suppress(Exception):
            current = read()
        for method, path, field in (
            ("PATCH", "/metricsCollection/serverUrl", "serverUrl"),
            ("PATCH", "/metricsCollection/pushInterval", "pushIntervalMs"),
            ("PUT", "/metricsCollection/toggle", "enableMetricCollection"),
        ):
            if current.get(field) != value[field]:
                _restore(pipeshub_client, method, path, f"{what} ({field})", {field: value[field]})

    with _saved(what, read, write) as before:
        yield before


@pytest.fixture(scope="module")
def contract_metrics_enabled(contract_metrics_collection: dict[str, Any]) -> str:
    if not contract_metrics_collection["enableMetricCollection"]:
        pytest.skip(
            "Metrics collection is off on this deployment. Each request that turns it off, "
            "also the one that puts the switch back, makes the server send its metrics to "
            "the collector."
        )
    return "on"


@pytest.fixture(scope="module")
def contract_web_search(pipeshub_client: PipeshubClient) -> Iterator[Any]:
    what = "web search settings and default provider"

    def read() -> dict[str, Any]:
        body = _get(pipeshub_client, "/web-search", what)
        # The built-in DuckDuckGo provider is the default when no stored provider is.
        default = next(
            provider["providerKey"] for provider in body["providers"] if provider.get("isDefault")
        )
        return {"settings": body["settings"], "default": default}

    def write(value: dict[str, Any]) -> None:
        _restore(pipeshub_client, "PUT", "/web-search/settings", what, value["settings"])
        if not _has(lambda: read()["default"], value["default"]):
            # For a stored provider the API first runs a search with it.
            _restore(pipeshub_client, "PUT", f"/web-search/default/{value['default']}", what)

    with _saved(what, read, write) as before:
        yield before


@pytest.fixture(scope="module")
def contract_duckduckgo_agents(pipeshub_client: PipeshubClient) -> str:
    """The API refuses to delete a stored provider while an agent searches with one of its kind.

    A stored DuckDuckGo provider, from a fixture or from a test case, could then
    not be removed again.
    """
    usage = response_body(
        pipeshub_client.request("GET", "/api/v1/agents/web-search-usage/duckduckgo"),
        (200,),
        "Read which agents use DuckDuckGo",
    )
    agents = usage.get("agents") or []
    if agents:
        pytest.skip(
            f"{len(agents)} agent(s) use DuckDuckGo web search. The API then refuses to delete "
            "a stored DuckDuckGo provider, so the run could not remove the ones it adds."
        )
    return "0"


@pytest.fixture(scope="module")
def contract_web_search_providers(
    pipeshub_client: PipeshubClient, contract_web_search: Any, contract_duckduckgo_agents: str
) -> Iterator[dict[str, str]]:
    # `contract_web_search` first: the first stored provider becomes the default, and its
    # teardown must come after the deletes below, which can change the default again.
    del contract_web_search, contract_duckduckgo_agents
    path = f"{_BASE}/web-search/providers"
    created: dict[str, str] = {}
    try:
        for role in PROVIDER_ROLES:
            # DuckDuckGo needs no API key. The API searches with it once before it stores it.
            resp = pipeshub_client.request(
                "POST", path, json={"provider": "duckduckgo", "configuration": {}}
            )
            details = response_body(resp, (200,), f"Add web search provider ({role})").get(
                "details"
            )
            assert isinstance(details, dict) and details.get("providerKey"), (
                f"Add web search provider ({role}): response has no details.providerKey"
            )
            created[role] = str(details["providerKey"])
        yield created
    finally:
        for role, key in created.items():
            delete_quietly(
                f"web search provider ({role})",
                lambda key=key: pipeshub_client.request("DELETE", f"{path}/{key}"),
            )


@pytest.fixture(scope="module")
def contract_slack_bots(user_session_client: SessionClient) -> Iterator[dict[str, str]]:
    path = f"{_BASE}/slack-bot"
    created: dict[str, str] = {}
    try:
        for role in ROLES:
            # The API stores the token and the secret as given; it does not call Slack.
            resp = user_session_client.request(
                "POST",
                path,
                json={
                    "name": f"contract-{role}-{uuid4().hex[:8]}",
                    "botToken": "contract-not-a-token",
                    "signingSecret": "contract-not-a-secret",
                },
            )
            config = response_body(resp, (200,), f"Create Slack bot configuration ({role})").get(
                "config"
            )
            assert isinstance(config, dict) and config.get("id"), (
                f"Create Slack bot configuration ({role}): response has no config.id"
            )
            created[role] = str(config["id"])
        yield created
    finally:
        for role, config_id in created.items():
            delete_quietly(
                f"Slack bot configuration ({role})",
                lambda config_id=config_id: user_session_client.request(
                    "DELETE", f"{path}/{config_id}"
                ),
            )


def _saved_source(fixture: str, key: str, what: str) -> ValueSource:
    return ValueSource(fixture, (key,), lambda before: (_state(before),), what)


_PUT_BACK = "read before the run and written back after this suite if a test case changed it"

# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# A `.saved` value says only whether the setting had a value; no value is a credential.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    _saved_source(
        "contract_smtp",
        "smtp.saved",
        f"The SMTP configuration, {_PUT_BACK}; if the deployment has none, the shared fixture "
        "`smtp_ready` first writes the one of the SMTP_* environment, which the API cannot "
        "remove again, and without that environment (or with HIDE_SECRET_CONFIG, which masks "
        "the configuration) the operation is skipped.",
    ),
    ValueSource(
        "contract_auth_configs",
        tuple(f"authConfig.{provider}.saved" for provider in AUTH_PROVIDERS),
        lambda before: tuple(_state(before[provider]) for provider in AUTH_PROVIDERS),
        "The configuration of each sign-in provider (Azure AD, Microsoft, Google, SAML SSO, "
        f"OAuth), {_PUT_BACK}; a provider that had none keeps a placeholder configuration "
        f"(`{_PLACEHOLDER}`, JIT off) after the run, because the API cannot remove one.",
    ),
    _saved_source(
        "contract_frontend_public_url",
        "frontendPublicUrl.saved",
        f"The public URL of the frontend, {_PUT_BACK}; it creates nothing, and the operation "
        "is skipped on a deployment that has none, because the API cannot remove it.",
    ),
    _saved_source(
        "contract_connector_public_url",
        "connectorPublicUrl.saved",
        f"The public URL of the connector service, {_PUT_BACK}; a deployment that had none "
        f"keeps `{_PLACEHOLDER_URL}` after the run, because the API cannot remove it.",
    ),
    _saved_source(
        "contract_platform_settings",
        "platformSettings.saved",
        f"The upload size limit and the feature flags, {_PUT_BACK}; a deployment that had "
        "stored none then has its defaults stored as explicit values.",
    ),
    _saved_source(
        "contract_system_prompts",
        "systemPrompts.saved",
        f"The three custom system prompts, {_PUT_BACK}; a deployment that had stored none "
        "then has the prompts that it showed before stored as explicit values.",
    ),
    ValueSource(
        "contract_metrics_collection",
        ("metricsCollection.saved", "metricsCollection.serverUrl"),
        lambda before: (_state(before), before["serverUrl"]),
        "The switch, the push interval and the server URL of the metrics collection, "
        f"{_PUT_BACK}; it creates nothing, and the server URL is the one that valid requests send.",
    ),
    ValueSource(
        "contract_metrics_enabled",
        ("metricsCollection.enabled",),
        lambda state: (state,),
        "That metrics collection is on; it creates nothing, and the operation is skipped on a "
        "deployment that has it off, because each request that turns it off makes the server "
        "send its metrics to the collector.",
    ),
    _saved_source(
        "contract_web_search",
        "webSearch.saved",
        "The web search settings (images) and which provider is the default, "
        f"{_PUT_BACK}; a deployment that had stored none then has the default settings stored.",
    ),
    ValueSource(
        "contract_duckduckgo_agents",
        ("duckduckgo.agentCount",),
        lambda count: (count,),
        "The number of agents that search with DuckDuckGo, which is 0; it creates nothing, and "
        "the operations are skipped on a deployment that has such an agent, because the API "
        "then refuses to delete a stored DuckDuckGo provider.",
    ),
    ValueSource(
        "contract_web_search_providers",
        tuple(f"webSearchProvider.{role}.key" for role in PROVIDER_ROLES),
        lambda providers: tuple(providers[role] for role in PROVIDER_ROLES),
        "Three stored DuckDuckGo web search providers (no API key): one to update, one to "
        "delete, one to make the default.",
    ),
    ValueSource(
        "contract_slack_bots",
        tuple(f"slackBot.{role}.id" for role in ROLES),
        lambda bots: tuple(bots[role] for role in ROLES),
        "Two Slack bot configurations with a made-up token and secret: one to update, one to "
        "delete.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
