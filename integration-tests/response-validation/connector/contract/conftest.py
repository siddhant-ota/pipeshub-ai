"""Fixtures for the connectors contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`connector.mutable.id`, `crawlConnector.paused.id`, `oauthConfig.readonly.id`, ...).

No fixture enables a connector, so nothing here starts a sync. Most connectors
are of the Web type, which needs no credential. The Slack connectors hold
made-up credentials, which the API stores as given and uses only in a sync.
Only the MinIO connector names a real source, the MinIO server of the
integration stack; without that server its fixture skips.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from helper.clients.kb_client import KBClient
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.contract.suite import load_suite
from helper.http.protocol import HTTPClientProtocol

logger = logging.getLogger("contract")

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# From the suite file, so that the fixtures and the requests name the same types: a crawl
# schedule is found by the type of its connector, an OAuth configuration by its connector type.
_CONSTANTS = load_suite(SUITE_PATH).constants
_WEB = _CONSTANTS["connector.type"]
_SLACK = _CONSTANTS["oauthConfig.connectorType"]
_MINIO = "MinIO"

_CONNECTORS = "/api/v1/connectors"
_OAUTH_CONFIGS = f"/api/v1/oauth/{_SLACK}"
_CRAWLING = f"/api/v1/crawlingManager/{_WEB}"
_CRAWL_SCHEDULES = "/api/v1/crawlingManager/schedule/all"
_GOOGLE_WORKSPACE_OAUTH_CONFIG = (
    "/api/v1/configurationManager/connectors/googleWorkspaceOauthConfig"
)
_DELETING = "DELETING"
# What a delete of a connector answers when one is already in progress.
_GONE_OR_GOING = (404, 409)

# `.invalid` never resolves (RFC 2606). A Web connector must reach its website before it can
# be enabled, so one with this URL stays disabled and never syncs, whatever a test case sends.
_WEB_CONFIG = {"sync": {"url": "https://contract-fixture.invalid/", "type": "single"}}
_API_TOKEN = {"auth": {"apiToken": "contract-not-a-real-token"}}
_OAUTH_CLIENT = {"clientId": "contract-client-id", "clientSecret": "contract-not-a-real-secret"}

# One connector per role, so that an update or a delete under test cannot change what another
# operation reads, in whatever order they run. `idle` takes the invalid requests of resync.
CONNECTOR_ROLES = ("readonly", "mutable", "disposable", "idle")
_DISPOSABLE = "disposable"
# `apiToken` is read; the toggle operation switches the agent use of `agentToggle` on and off.
API_TOKEN_ROLES = ("apiToken", "agentToggle")
# One connector per state of a crawl schedule: `scheduled` is read, `removable` loses its
# schedule, `pausable` is paused, `paused` already is, to be resumed. `removedWithAll` keeps
# its schedule until the operation that removes every schedule, which is sent last.
# `schedulable` has no schedule; the test cases give it theirs.
_PAUSED = "paused"
_WITH_SCHEDULE = ("scheduled", "removable", "pausable", _PAUSED, "removedWithAll")
CRAWL_ROLES = (*_WITH_SCHEDULE, "schedulable")
OAUTH_CONFIG_ROLES = ("readonly", "mutable", "disposable")
KNOWLEDGE_BASE_ROLES = ("readonly", "empty")

_SCHEDULE_IN_DAYS = 30


def _create_connector(
    client: HTTPClientProtocol, role: str, connector_type: str = _WEB, **fields: Any
) -> str:
    what = f"Create {connector_type} connector ({role})"
    resp = client.request(
        "POST",
        f"{_CONNECTORS}/",
        json={
            "connectorType": connector_type,
            "instanceName": f"contract-{role}-{uuid4().hex[:8]}",
            "scope": "team",
            "config": _WEB_CONFIG,
            **fields,
        },
    )
    connector = response_body(resp, (200, 201), what).get("connector") or {}
    assert connector.get("connectorId"), f"{what}: response has no connector.connectorId"
    return str(connector["connectorId"])


def _delete_connectors(
    client: HTTPClientProtocol, connectors: dict[str, str], maybe_in_deletion: tuple[str, ...] = ()
) -> None:
    """`maybe_in_deletion`: the roles that an operation under test may have deleted already.

    The API deletes a connector in the background and answers 409 to a second delete until
    it is gone.
    """
    for role, connector_id in connectors.items():
        delete_quietly(
            f"connector ({role})",
            lambda connector_id=connector_id: client.request(
                "DELETE", f"{_CONNECTORS}/{connector_id}"
            ),
            also_fine=_GONE_OR_GOING if role in maybe_in_deletion else (404,),
        )


def _connectors_named(client: HTTPClientProtocol, prefix: str) -> dict[str, str]:
    """ID -> name of every connector the client may see whose name starts with `prefix`
    and that is not being deleted."""
    found: dict[str, str] = {}
    for scope in ("team", "personal"):
        page = 1
        while True:
            body = response_body(
                client.request(
                    "GET", f"{_CONNECTORS}/", params={"scope": scope, "page": page, "limit": 200}
                ),
                (200,),
                f"List the connectors ({scope})",
            )
            for connector in body.get("connectors") or []:
                name = str(connector.get("name") or "")
                if (
                    connector.get("_key")
                    and name.startswith(prefix)
                    and connector.get("status") != _DELETING
                ):
                    found[str(connector["_key"])] = name
            if not (body.get("pagination") or {}).get("hasNext"):
                break
            page += 1
    return found


def _create_oauth_connector(client: HTTPClientProtocol, role: str) -> str:
    """A Slack connector that signs in with OAuth, created with the client of an OAuth app.

    For an admin the API then also makes an OAuth configuration with that client
    (create_connector_instance in router.py). It is the only way to one: the gateway
    refuses every request to POST /oauth/{connectorType} (see `no_success_response`).
    """
    return _create_connector(
        client,
        role,
        _SLACK,
        # Slack connectors are personal only.
        scope="personal",
        authType="OAUTH",
        baseUrl=client.base_url,
        config={
            "auth": {**_OAUTH_CLIENT, "oauthInstanceName": f"contract-{role}-{uuid4().hex[:8]}"}
        },
    )


def _oauth_config_id(client: HTTPClientProtocol, connector_id: str, role: str) -> str:
    what = f"Read the configuration of the OAuth connector ({role})"
    resp = client.request("GET", f"{_CONNECTORS}/{connector_id}/config")
    config = response_body(resp, (200,), what).get("config") or {}
    config_id = ((config.get("config") or {}).get("auth") or {}).get("oauthConfigId")
    assert config_id, f"{what}: the API made no OAuth configuration for the connector"
    return str(config_id)


def _delete_oauth_configs(client: HTTPClientProtocol, configs: dict[str, str]) -> None:
    for role, config_id in configs.items():
        delete_quietly(
            f"OAuth configuration ({role})",
            lambda config_id=config_id: client.request("DELETE", f"{_OAUTH_CONFIGS}/{config_id}"),
        )


@pytest.fixture(scope="module")
def contract_case_names(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    """The name of what a test case creates or renames, and the cleanup that goes with it.

    The API refuses a connector name that is in use, so every request gets its own:
    `{case}` becomes the number of the request (helper/contract/values.py).

    After the run, this deletes every connector that still has a name of this run. The run
    deletes what a create answered with a 2xx. But the connector service stores a new
    instance before it stores its configuration (create_connector_instance in router.py):
    a configuration that it cannot store, for example `config.auth` that is not an object,
    is answered with 500, and the instance stays, with no ID in the response.
    """
    prefix = f"contract-case-{uuid4().hex[:8]}-"
    try:
        yield prefix + "{case}"
    finally:
        left_behind: dict[str, str] = {}
        try:
            left_behind = _connectors_named(pipeshub_client, prefix)
        except Exception as exc:  # noqa: BLE001 - logged and not raised, as in delete_quietly
            logger.warning("Could not look for the connectors that the run left behind: %s", exc)
        by_name = {f"left by the run, {name}": id_ for id_, name in left_behind.items()}
        _delete_connectors(pipeshub_client, by_name, maybe_in_deletion=tuple(by_name))


@pytest.fixture(scope="module")
def contract_no_google_workspace_credentials(pipeshub_client: HTTPClientProtocol) -> str:
    """Lets getTokenFromCode run only where it calls nobody.

    The route has no validator: it sends the code of every request, valid or not, to Google
    together with the Google Workspace OAuth client of the organization. Without such a
    client it answers 404 before that, which is the case on a test deployment.
    """
    config = response_body(
        pipeshub_client.request("GET", _GOOGLE_WORKSPACE_OAUTH_CONFIG),
        (200,),
        "Read the Google Workspace OAuth configuration",
    )
    if config.get("clientId"):
        pytest.skip(
            "The organization has Google Workspace OAuth credentials, so every request to "
            "this operation would be a token request to Google."
        )
    return "none"


@pytest.fixture(scope="module")
def contract_no_crawl_schedules_of_others(pipeshub_client: HTTPClientProtocol) -> str:
    """Lets removeAllCrawlingJob run only where the schedules of this suite are the only ones.

    The operation removes every crawl schedule of the organization, and nothing puts the
    schedule of a real connector back. This looks before `contract_crawl_connectors` makes
    the schedules of the suite, so whatever the list has then belongs to someone else.
    """
    what = "List the crawl schedules"
    schedules = response_body(pipeshub_client.request("GET", _CRAWL_SCHEDULES), (200,), what).get(
        "data"
    )
    assert isinstance(schedules, list), f"{what}: `data` is not a list: {schedules!r}"
    if schedules:
        pytest.skip(
            f"The organization has {len(schedules)} crawl job(s) that this suite did not make. "
            "This operation would remove them, and nothing puts them back."
        )
    return "0"


@pytest.fixture(scope="module")
def contract_connectors(pipeshub_client: HTTPClientProtocol) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in CONNECTOR_ROLES:
            created[role] = _create_connector(pipeshub_client, role)
        yield created
    finally:
        _delete_connectors(pipeshub_client, created, maybe_in_deletion=(_DISPOSABLE,))


@pytest.fixture(scope="module")
def contract_session_connector(user_session_client: HTTPClientProtocol) -> Iterator[str]:
    created: dict[str, str] = {}
    try:
        created["sessionOwned"] = _create_connector(
            user_session_client, "session-owned", scope="personal"
        )
        yield created["sessionOwned"]
    finally:
        _delete_connectors(user_session_client, created)


@pytest.fixture(scope="module")
def contract_api_token_connectors(pipeshub_client: HTTPClientProtocol) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in API_TOKEN_ROLES:
            created[role] = _create_connector(
                pipeshub_client,
                role,
                _SLACK,
                # Slack connectors are personal only.
                scope="personal",
                authType="API_TOKEN",
                config=_API_TOKEN,
            )
        yield created
    finally:
        _delete_connectors(pipeshub_client, created)


@pytest.fixture(scope="module")
def contract_oauth_connector(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    """The API builds the authorization URL from the stored client ID; it calls the third
    party only when a sign-in comes back with a code."""
    client = pipeshub_client
    connectors: dict[str, str] = {}
    configs: dict[str, str] = {}
    try:
        connectors["oauth"] = _create_oauth_connector(client, "oauth")
        configs["oauth"] = _oauth_config_id(client, connectors["oauth"], "oauth")
        yield connectors["oauth"]
    finally:
        _delete_connectors(client, connectors)
        _delete_oauth_configs(client, configs)


@pytest.fixture(scope="module")
def contract_minio_connector(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    """A MinIO connector that is not enabled.

    The options of a dynamic filter come from the source, so this is the one fixture that
    names a real one: the MinIO server of the integration stack, with the variables of the
    MinIO connector suite and its defaults for that stack.

    The check below reaches the server from the test process, on its published port. The
    connector service reaches it by its own address (`MINIO_CONNECTOR_ENDPOINT`), which
    this process cannot check.
    """
    # Here and not at the top: the conftest of the MinIO suite imports the backend package,
    # which is on the path only under pytest (root conftest), and
    # `python -m helper.contract plan` imports this file without pytest.
    from connectors.minio import conftest as minio_suite
    from connectors.minio.minio_storage_helper import MinioStorageHelper

    endpoint = os.getenv("MINIO_TEST_ENDPOINT", minio_suite.DEFAULT_ENDPOINT)
    access_key = os.getenv("MINIO_ROOT_USER", minio_suite.DEFAULT_ACCESS_KEY)
    secret_key = os.getenv("MINIO_ROOT_PASSWORD", minio_suite.DEFAULT_SECRET_KEY)
    try:
        MinioStorageHelper(access_key, secret_key, endpoint_url=endpoint).list_objects(
            os.getenv("MINIO_TEST_BUCKET", minio_suite.DEFAULT_BUCKET)
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        pytest.skip(
            f"The MinIO server of the integration stack is not reachable at {endpoint}: {exc}"
        )

    created: dict[str, str] = {}
    try:
        created["dynamicFilter"] = _create_connector(
            pipeshub_client,
            "dynamic-filter",
            _MINIO,
            scope="personal",
            config={
                "auth": {
                    "endpointUrl": os.getenv("MINIO_CONNECTOR_ENDPOINT", "http://minio:9000"),
                    "accessKey": access_key,
                    "secretKey": secret_key,
                    "useSsl": False,
                    "verifySsl": False,
                }
            },
        )
        yield created["dynamicFilter"]
    finally:
        _delete_connectors(pipeshub_client, created)


@pytest.fixture(scope="module")
def contract_crawl_connectors(pipeshub_client: HTTPClientProtocol) -> Iterator[dict[str, str]]:
    client = pipeshub_client
    # A one-time schedule far ahead: it is a schedule in every respect, and it does not run.
    run_at = datetime.now(UTC) + timedelta(days=_SCHEDULE_IN_DAYS)
    schedule = {
        "scheduleConfig": {
            "scheduleType": "once",
            "isEnabled": True,
            "timezone": "UTC",
            "scheduledTime": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    created: dict[str, str] = {}
    try:
        for role in CRAWL_ROLES:
            created[role] = _create_connector(client, f"crawl-{role}")
        for role in _WITH_SCHEDULE:
            response_body(
                client.request("POST", f"{_CRAWLING}/{created[role]}/schedule", json=schedule),
                (200, 201),
                f"Schedule a crawl ({role})",
            )
        response_body(
            client.request("POST", f"{_CRAWLING}/{created[_PAUSED]}/pause"),
            (200,),
            "Pause the crawl schedule",
        )
        yield created
    finally:
        # The schedule first: it lives in the job queue, and must not outlive a connector
        # whose delete failed. `schedulable` has the last schedule that a test case gave it;
        # if that one comes due before this, the connector service skips the sync of a
        # connector that is not enabled.
        for role, connector_id in created.items():
            delete_quietly(
                f"crawl schedule ({role})",
                lambda connector_id=connector_id: client.request(
                    "DELETE", f"{_CRAWLING}/{connector_id}/remove"
                ),
            )
        _delete_connectors(client, created)


@pytest.fixture(scope="module")
def contract_oauth_configs(user_session_client: HTTPClientProtocol) -> Iterator[dict[str, str]]:
    """One OAuth configuration per role, each made together with a connector of its own."""
    client = user_session_client
    connectors: dict[str, str] = {}
    configs: dict[str, str] = {}
    try:
        for role in OAUTH_CONFIG_ROLES:
            connectors[role] = _create_oauth_connector(client, f"oauth-config-{role}")
            configs[role] = _oauth_config_id(client, connectors[role], role)
        yield configs
    finally:
        _delete_connectors(client, connectors)
        _delete_oauth_configs(client, configs)


@pytest.fixture(scope="module")
def contract_knowledge_bases(kb_client: KBClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in KNOWLEDGE_BASE_ROLES:
            kb = kb_client.create_kb(name=f"contract-{role}-{uuid4().hex[:8]}")
            assert kb.get("id"), f"Create knowledge base ({role}): response has no id: {kb}"
            created[role] = str(kb["id"])
        yield created
    finally:
        for role, kb_id in created.items():
            delete_quietly(
                f"knowledge base ({role})", lambda kb_id=kb_id: kb_client.delete(f"/{kb_id}")
            )


@pytest.fixture(scope="module")
def contract_record(kb_client: KBClient, contract_knowledge_bases: dict[str, str]) -> str:
    """A text file in the readonly knowledge base. Deleting the knowledge base deletes it.

    The fixture does not wait for the indexing: the content route answers 200 for a record
    that the caller may read, with `No record found` as its content until it is indexed.
    """
    upload = kb_client.upload_file(
        contract_knowledge_bases["readonly"],
        f"contract-readonly-{uuid4().hex[:8]}.txt",
        b"A record for the contract tests of the connector routes.\n",
        mimetype="text/plain",
    )
    record_id = (upload.get("records") or [{}])[0].get("recordId")
    assert record_id, f"Upload a record: response has no recordId: {upload}"
    return str(record_id)


def _by_role(fixture: str, prefix: str, roles: tuple[str, ...], what: str) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.id" for role in roles),
        lambda by_role: tuple(by_role[role] for role in roles),
        what,
    )


def _one_connector(fixture: str, role: str, what: str) -> ValueSource:
    return ValueSource(
        fixture, (f"connector.{role}.id",), lambda connector_id: (connector_id,), what
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    # First, so that it finishes last. pytest finishes the fixtures of a module in the reverse
    # order of their setup: `contract_values` sets these up in this order, and `contract_run`
    # after them. So the run has deleted what its test cases created, and the other fixtures
    # their own connectors, before this one looks for what is left.
    ValueSource(
        "contract_case_names",
        ("testCase.uniqueName",),
        lambda name: (name,),
        "Creates nothing. The name that the test cases give to the connectors they create or "
        "rename: the API refuses a name that is in use, so the name has eight random "
        "characters for this run and the number of the request. After the run it deletes "
        "every connector that still has such a name: the API can keep the connector of a "
        "create that it answers with 500.",
    ),
    ValueSource(
        "contract_no_google_workspace_credentials",
        ("googleWorkspace.oauthClient",),
        lambda state: (state,),
        "Creates nothing. It reads the Google Workspace OAuth configuration of the "
        "organization and skips when there is one: the token exchange under test would send "
        "every request to Google with it.",
    ),
    _by_role(
        "contract_connectors",
        "connector",
        CONNECTOR_ROLES,
        "Four Web connectors of team scope that are not enabled: to read, to update, to delete, "
        "and one that takes the invalid requests of resync. Their website is a host name that "
        "does not exist, so none of them can be enabled or synced.",
    ),
    _one_connector(
        "contract_session_connector",
        "sessionOwned",
        "One more such Web connector, of personal scope, created with the session of the test "
        "user: the spec lists only that login for the operation that saves filters, so the run "
        "uses it, and the connector belongs to the user of that login.",
    ),
    _by_role(
        "contract_api_token_connectors",
        "connector",
        API_TOKEN_ROLES,
        "Two personal Slack connectors with a made-up API token, not enabled. One is read: the "
        "filter options are answered only for a connector that has stored credentials. On the "
        "other the toggle operation switches the agent use on and off, which starts no sync.",
    ),
    _one_connector(
        "contract_oauth_connector",
        "oauth",
        "A personal Slack connector that signs in with OAuth, not enabled, created with a "
        "made-up client ID and secret, and the Slack OAuth configuration that the API makes "
        "with them: the authorization URL is built from them without a call to Slack.",
    ),
    _one_connector(
        "contract_minio_connector",
        "dynamicFilter",
        "A personal MinIO connector for the MinIO server of the integration stack, not enabled: "
        "the options of its `buckets` filter are read from that server. It skips when the "
        "test process cannot reach the server on its published port; the connector service "
        "reaches the server by another address (`http://minio:9000` in the stack), which the "
        "fixture cannot check.",
    ),
    # Before the crawl connectors: it must see the schedules that exist without them.
    ValueSource(
        "contract_no_crawl_schedules_of_others",
        ("crawlSchedule.ofOthers.count",),
        lambda count: (count,),
        "Creates nothing. It lists the crawl schedules of the organization before the suite "
        "makes its own, and skips when there is one: the operation that removes every crawl "
        "schedule would remove it, and nothing puts the schedule of a real connector back.",
    ),
    _by_role(
        "contract_crawl_connectors",
        "crawlConnector",
        CRAWL_ROLES,
        "Six more Web connectors that are not enabled, one per state of a crawl schedule: "
        "four with a one-time schedule 30 days ahead (to read, to remove, to pause, and one "
        "for the operation that removes every crawl schedule of the organization, which is "
        "sent last), one with such a schedule already paused (to resume), and one without a "
        "schedule, which gets the schedules of the test cases.",
    ),
    _by_role(
        "contract_oauth_configs",
        "oauthConfig",
        OAUTH_CONFIG_ROLES,
        "Three Slack OAuth configurations with a made-up client ID and secret: to read, to "
        "update, to delete. The gateway refuses to create one by itself, so each is made by "
        "the API together with a personal Slack OAuth connector, with the session of the test "
        "user; those three connectors are not enabled and are deleted with them.",
    ),
    _by_role(
        "contract_knowledge_bases",
        "knowledgeBase",
        KNOWLEDGE_BASE_ROLES,
        "Two knowledge bases: one to browse, and one that stays empty, so that a reindex of it "
        "has no record to index.",
    ),
    ValueSource(
        "contract_record",
        ("knowledgeBase.recordId",),
        lambda record_id: (record_id,),
        "A small text file uploaded to the knowledge base to browse; its content is read.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
