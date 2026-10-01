"""Fixtures for the connectors contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`connector.mutable.id`, `crawlConnector.paused.id`, `oauthConfig.readonly.id`, ...).

No fixture enables a connector, so nothing here starts a sync. Most connectors
are of the Web type, which needs no credential. The two Slack connectors hold
made-up credentials, which the API stores as given and uses only in a sync.
Only the MinIO connector names a real source, the MinIO server of the
integration stack; without that server its fixture is skipped.
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
from helper.contract.events import read_cases
from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.runner import run_files
from helper.contract.sources import ValueSource
from helper.contract.suite import load_suite
from helper.http.protocol import HTTPClientProtocol

logger = logging.getLogger("contract")

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# From the suite file, so that the fixtures and the requests name the same types: a crawl
# schedule is found by the type of its connector, an OAuth configuration by its connector type.
_CONSTANTS = load_suite(SUITE_PATH).constants
_WEB = _CONSTANTS["connector.type"]
_OAUTH_TYPE = _CONSTANTS["oauthConfig.connectorType"]
_MINIO = "MinIO"

_CONNECTORS = "/api/v1/connectors"
_OAUTH_CONFIGS = f"/api/v1/oauth/{_OAUTH_TYPE}"
_CRAWLING = f"/api/v1/crawlingManager/{_WEB}"
_CRAWL_SCHEDULES = "/api/v1/crawlingManager/schedule/all"
_GOOGLE_WORKSPACE_OAUTH_CONFIG = (
    "/api/v1/configurationManager/connectors/googleWorkspaceOauthConfig"
)
# createConnectorInstance, as Schemathesis names it in the events of a run.
_CREATE = "POST /connectors"
_DELETING = "DELETING"

# `.invalid` never resolves (RFC 2606). A Web connector must reach its website before it can
# be enabled, so one with this URL stays disabled and never syncs, whatever a test case sends.
_WEB_CONFIG = {"sync": {"url": "https://contract-fixture.invalid/", "type": "single"}}
_API_TOKEN = {"auth": {"apiToken": "contract-not-a-real-token"}}
_OAUTH_CLIENT = {"clientId": "contract-client-id", "clientSecret": "contract-not-a-real-secret"}

# One connector per role, so that an update or a delete under test cannot change what another
# operation reads, in whatever order they run. `idle` takes the invalid requests of the
# operations that start a sync.
CONNECTOR_ROLES = ("readonly", "mutable", "disposable", "idle")
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


def _delete_connectors(client: HTTPClientProtocol, connectors: dict[str, str]) -> None:
    for role, connector_id in connectors.items():
        delete_quietly(
            f"connector ({role})",
            lambda connector_id=connector_id: client.request(
                "DELETE", f"{_CONNECTORS}/{connector_id}"
            ),
        )


def _web_connectors(client: HTTPClientProtocol) -> dict[str, dict[str, Any]]:
    """Every Web connector that the client may see, by ID."""
    found: dict[str, dict[str, Any]] = {}
    for scope in ("team", "personal"):
        page = 1
        while True:
            body = response_body(
                client.request(
                    "GET",
                    f"{_CONNECTORS}/",
                    params={"scope": scope, "connectorType": _WEB, "page": page, "limit": 200},
                ),
                (200,),
                f"List the {_WEB} connectors ({scope})",
            )
            for connector in body.get("connectors") or []:
                if connector.get("_key"):
                    found[str(connector["_key"])] = connector
            if not (body.get("pagination") or {}).get("hasNext"):
                break
            page += 1
    return found


def _names_of_failed_creates() -> set[str]:
    """The instance name in every create request of this run that was not answered with a 2xx."""
    events = run_files(load_suite(SUITE_PATH)).events
    names: set[str] = set()
    if not events.exists():
        return names
    for case in read_cases(events):
        if case.label != _CREATE or (case.status is not None and 200 <= case.status < 300):
            continue
        body = case.request_json()
        name = body.get("instanceName") if isinstance(body, dict) else None
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _create_oauth_config(client: HTTPClientProtocol, role: str) -> str:
    what = f"Create {_OAUTH_TYPE} OAuth configuration ({role})"
    resp = client.request(
        "POST",
        _OAUTH_CONFIGS,
        json={
            "oauthInstanceName": f"contract-{role}-{uuid4().hex[:8]}",
            "config": _OAUTH_CLIENT,
            # The gateway requires it; the API builds the redirect URI of the sign-in from it.
            "baseUrl": client.base_url,
        },
    )
    config = response_body(resp, (200, 201), what).get("oauthConfig") or {}
    assert config.get("_id"), f"{what}: response has no oauthConfig._id"
    return str(config["_id"])


def _delete_oauth_configs(client: HTTPClientProtocol, configs: dict[str, str]) -> None:
    for role, config_id in configs.items():
        delete_quietly(
            f"OAuth configuration ({role})",
            lambda config_id=config_id: client.request("DELETE", f"{_OAUTH_CONFIGS}/{config_id}"),
        )


@pytest.fixture(scope="module")
def contract_failed_creates(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    """Deletes, after the run, the connectors that a failed create left behind.

    The connector service stores a new instance before it stores its configuration
    (create_connector_instance in app/connectors/api/router.py). A configuration that it
    cannot store, for example `config.auth` that is not an object, is answered with 500,
    and the instance stays. That response has no ID, so `created_resources` cannot delete
    it. Such a connector is new, is of the Web type, and has the name of a create request
    of this run that was not answered with a 2xx.
    """
    client = pipeshub_client
    before = set(_web_connectors(client))
    try:
        yield str(len(before))
    finally:
        left_behind: list[str] = []
        try:
            names = _names_of_failed_creates()
            left_behind = [
                connector_id
                for connector_id, connector in _web_connectors(client).items()
                if connector_id not in before
                and connector.get("name") in names
                and connector.get("status") != _DELETING
            ]
        except Exception as exc:  # noqa: BLE001 - logged and not raised, as in delete_quietly
            logger.warning("Could not look for the connectors of failed creates: %s", exc)
        _delete_connectors(client, {f"left by a failed create, {id_}": id_ for id_ in left_behind})


@pytest.fixture(scope="module")
def contract_unique_name() -> str:
    """The name of what a test case creates or renames; the API refuses a name twice.

    `{case}` becomes the number of the request (helper/contract/values.py).
    """
    return f"contract-case-{uuid4().hex[:8]}-{{case}}"


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
    schedules = response_body(
        pipeshub_client.request("GET", _CRAWL_SCHEDULES), (200,), "List the crawl schedules"
    ).get("data")
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
        _delete_connectors(pipeshub_client, created)


@pytest.fixture(scope="module")
def contract_session_connector(user_session_client: HTTPClientProtocol) -> Iterator[str]:
    created: dict[str, str] = {}
    try:
        created["sessionOwned"] = _create_connector(user_session_client, "session-owned")
        yield created["sessionOwned"]
    finally:
        _delete_connectors(user_session_client, created)


@pytest.fixture(scope="module")
def contract_api_token_connector(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    created: dict[str, str] = {}
    try:
        # Slack connectors are personal only.
        created["apiToken"] = _create_connector(
            pipeshub_client,
            "api-token",
            _OAUTH_TYPE,
            scope="personal",
            authType="API_TOKEN",
            config=_API_TOKEN,
        )
        yield created["apiToken"]
    finally:
        _delete_connectors(pipeshub_client, created)


@pytest.fixture(scope="module")
def contract_oauth_connector(pipeshub_client: HTTPClientProtocol) -> Iterator[str]:
    """A connector that signs in with OAuth, linked to an OAuth configuration of its own.

    The API builds the authorization URL from the stored client ID; it calls the third
    party only when a sign-in comes back with a code.
    """
    client = pipeshub_client
    configs: dict[str, str] = {}
    connectors: dict[str, str] = {}
    try:
        configs["connector"] = _create_oauth_config(client, "connector")
        connectors["oauth"] = _create_connector(
            client,
            "oauth",
            _OAUTH_TYPE,
            scope="personal",
            authType="OAUTH",
            config={"auth": {"oauthConfigId": configs["connector"]}},
        )
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
                    # As the connector service reaches it, inside the compose network.
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
        # whose delete failed.
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
    created: dict[str, str] = {}
    try:
        for role in OAUTH_CONFIG_ROLES:
            created[role] = _create_oauth_config(user_session_client, role)
        yield created
    finally:
        _delete_oauth_configs(user_session_client, created)


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
        "contract_failed_creates",
        ("webConnector.existing.count",),
        lambda count: (count,),
        "Creates nothing. It counts the Web connectors before the run, and after the run it "
        "deletes the new Web connectors that have the name of a create request which the API "
        "did not answer with a 2xx: the API can keep the connector of a create that it answers "
        "with 500.",
    ),
    ValueSource(
        "contract_unique_name",
        ("testCase.uniqueName",),
        lambda name: (name,),
        "Creates nothing. The name that the test cases give to the connectors and OAuth "
        "configurations they create or rename: the API refuses a name that is in use, so the "
        "name has eight random characters for this run and the number of the request.",
    ),
    ValueSource(
        "contract_no_google_workspace_credentials",
        ("googleWorkspace.oauthClient",),
        lambda state: (state,),
        "Creates nothing. It reads the Google Workspace OAuth configuration of the "
        "organization and is skipped when there is one: the token exchange under test would "
        "send every request to Google with it.",
    ),
    _by_role(
        "contract_connectors",
        "connector",
        CONNECTOR_ROLES,
        "Four Web connectors of team scope that are not enabled: to read, to update, to delete, "
        "and one that takes the invalid requests of toggle and resync. Their website is a "
        "host name that does not exist, so none of them can be enabled or synced.",
    ),
    _one_connector(
        "contract_session_connector",
        "sessionOwned",
        "One more such Web connector, created with the session of the test user, for the "
        "operation that accepts only that login.",
    ),
    _one_connector(
        "contract_api_token_connector",
        "apiToken",
        "A personal Slack connector with a made-up API token, not enabled: the filter options "
        "are answered only for a connector that has stored credentials.",
    ),
    _one_connector(
        "contract_oauth_connector",
        "oauth",
        "A personal Slack connector that signs in with OAuth, not enabled, and the Slack OAuth "
        "configuration it uses, with a made-up client ID and secret: the authorization URL is "
        "built from them without a call to Slack.",
    ),
    _one_connector(
        "contract_minio_connector",
        "dynamicFilter",
        "A personal MinIO connector for the MinIO server of the integration stack, not enabled: "
        "the options of its `buckets` filter are read from that server. Skipped when the "
        "server is not reachable.",
    ),
    # Before the crawl connectors: it must see the schedules that exist without them.
    ValueSource(
        "contract_no_crawl_schedules_of_others",
        ("crawlSchedule.ofOthers.count",),
        lambda count: (count,),
        "Creates nothing. It lists the crawl schedules of the organization before the suite "
        "makes its own, and is skipped when there is one: the operation that removes every "
        "crawl schedule would remove it, and nothing puts the schedule of a real connector "
        "back.",
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
        "Three Slack OAuth configurations with a made-up client ID and secret, created with "
        "the session of the test user: to read, to update, to delete.",
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
