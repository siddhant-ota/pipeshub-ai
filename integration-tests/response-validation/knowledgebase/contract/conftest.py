"""Fixtures for the knowledge-base contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`knowledgeBase.mutable.id`, `record.readonly.id`, ...).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from helper.clients.kb_client import KBClient
from helper.clients.teams_client import TeamsClient
from helper.clients.users_client import UsersClient
from helper.contract.pytest_support import (
    delete_quietly,
    response_body,
    restore_quietly,
    suite_fixtures,
)
from helper.contract.sources import ValueSource
from helper.http.session_client import SessionClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")

_KB_PATH = "/api/v1/knowledgeBase"

# One resource per role, so that an update or a delete under test cannot change
# what another operation reads, in whatever order they run.
# `target` receives what createFolder and uploadRecords make.
KB_ROLES = ("readonly", "mutable", "disposable", "target")
# Each is shared with the grantee: only to list, to change the role, to revoke.
SHARED_KB_ROLES = ("sharedReadonly", "shared", "revocable")
# Folder or record role -> role of the knowledge base that holds it.
FOLDER_HOMES = {
    "mutable": "mutable",
    "disposable": "mutable",
    "moveTarget": "mutable",
    "reindexable": "mutable",
    "parent": "target",
}
RECORD_HOMES = {
    "readonly": "readonly",
    "mutable": "mutable",
    "disposable": "mutable",
    "movable": "mutable",
}

_WAIT_TIMEOUT_SEC = 60
_WAIT_INTERVAL_SEC = 2


def _name(role: str) -> str:
    return f"contract-{role}-{uuid4().hex[:8]}"


def _wait_for(what: str, is_there: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SEC
    while not is_there():
        assert time.monotonic() < deadline, f"{what}: not there after {_WAIT_TIMEOUT_SEC} s"
        time.sleep(_WAIT_INTERVAL_SEC)


def _new_id(body: dict[str, Any], what: str) -> str:
    assert body.get("id"), f"{what}: response has no id"
    return str(body["id"])


def _create_kb(kb_client: KBClient, role: str) -> str:
    what = f"Create knowledge base ({role})"
    resp = kb_client.post("/", json={"kbName": _name(role)})
    return _new_id(response_body(resp, (200, 201), what), what)


def _delete_kbs(kb_client: KBClient, kb_ids: dict[str, str]) -> None:
    for role, kb_id in kb_ids.items():
        delete_quietly(
            f"knowledge base ({role})", lambda kb_id=kb_id: kb_client.delete(f"/{kb_id}")
        )


def _create_folder(kb_client: KBClient, kb_id: str, role: str) -> str:
    what = f"Create folder ({role})"
    resp = kb_client.post(f"/{kb_id}/folder", json={"folderName": _name(role)})
    return _new_id(response_body(resp, (200, 201), what), what)


@pytest.fixture(scope="module")
def contract_knowledge_bases(kb_client: KBClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in KB_ROLES:
            created[role] = _create_kb(kb_client, role)
        yield created
    finally:
        _delete_kbs(kb_client, created)


# The two fixtures below have no teardown: deleting a knowledge base deletes its
# folders and records.
@pytest.fixture(scope="module")
def contract_folders(
    kb_client: KBClient, contract_knowledge_bases: dict[str, str]
) -> dict[str, str]:
    # So that reading the readonly knowledge base returns a folder.
    _create_folder(kb_client, contract_knowledge_bases["readonly"], "readonly")
    return {
        role: _create_folder(kb_client, contract_knowledge_bases[home], role)
        for role, home in FOLDER_HOMES.items()
    }


@pytest.fixture(scope="module")
def contract_records(
    kb_client: KBClient, contract_knowledge_bases: dict[str, str]
) -> dict[str, str]:
    """One small text file for each role. The operations need the records, not their index."""
    records: dict[str, str] = {}
    for role, home in RECORD_HOMES.items():
        uploaded = kb_client.upload_file(
            contract_knowledge_bases[home],
            f"{_name(role)}.txt",
            f"Contract test record ({role}).\n".encode(),
        )
        record_id = uploaded["records"][0].get("recordId")
        assert record_id, f"Upload record ({role}): no recordId in {uploaded}"
        records[role] = str(record_id)
    for role, record_id in records.items():
        _wait_for(
            f"record ({role})",
            lambda record_id=record_id: kb_client.get(f"/record/{record_id}").status_code == 200,
        )
    return records


@pytest.fixture(scope="module")
def contract_text_file(tmp_path_factory: pytest.TempPathFactory) -> str:
    # `.txt`, like the records above: the API replaces the file of a record only with a
    # file of the same extension.
    path = tmp_path_factory.mktemp("contract") / f"{_name('file')}.txt"
    path.write_text("Contract test file.\n", encoding="utf-8")
    return str(path)


def _is_in_graph(users_client: UsersClient, email: str) -> bool:
    resp = users_client.graph_list(search=email.split("@", 1)[0], limit=50)
    if resp.status_code != 200:
        return False
    users = resp.json().get("users") or []
    return any(str(user.get("email") or "").lower() == email for user in users)


@pytest.fixture(scope="module")
def contract_grantee(
    users_client: UsersClient, teams_client: TeamsClient
) -> Iterator[dict[str, str]]:
    name = _name("grantee")
    email = f"{name}@test-pipeshub.com"
    created: dict[str, str] = {}
    try:
        user = response_body(
            users_client.create_user(email=email, full_name=name), (200, 201), "Create user"
        )
        user_id = user.get("_id") or user.get("id")
        assert user_id, "Create user: response has no _id"
        created["user"] = str(user_id)
        # The user reaches the graph over the message broker. A knowledge base cannot be
        # shared with a user who is not there yet.
        _wait_for("user in the graph", lambda: _is_in_graph(users_client, email))

        team = response_body(teams_client.create_team(name), (200, 201), "Create team")
        created["team"] = _new_id(team.get("data") or {}, "Create team")
        yield created
    finally:
        if "team" in created:
            delete_quietly("team", lambda: teams_client.delete_team(created["team"]))
        if "user" in created:
            delete_quietly("user", lambda: users_client.delete_user(created["user"]))


@pytest.fixture(scope="module")
def contract_shared_knowledge_bases(
    kb_client: KBClient, contract_grantee: dict[str, str]
) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in SHARED_KB_ROLES:
            created[role] = _create_kb(kb_client, role)
            resp = kb_client.post(
                f"/{created[role]}/permissions",
                json={
                    "userIds": [contract_grantee["user"]],
                    "teamIds": [contract_grantee["team"]],
                    "role": "READER",
                },
            )
            result = response_body(resp, (201,), f"Share knowledge base ({role})")
            granted = (result.get("permissionResult") or {}).get("grantedCount")
            assert granted == 2, f"Share knowledge base ({role}): granted {granted}, expected 2"
        yield created
    finally:
        _delete_kbs(kb_client, created)


@pytest.fixture(scope="module")
def contract_unshared_knowledge_base(user_session_client: SessionClient) -> Iterator[str]:
    """A knowledge base of the session user.

    createKBPermission accepts only a session login, and only the owner of a
    knowledge base may share it.
    """
    what = "Create knowledge base (unshared)"
    resp = user_session_client.request("POST", f"{_KB_PATH}/", json={"kbName": _name("unshared")})
    kb_id = _new_id(response_body(resp, (200, 201), what), what)
    try:
        yield kb_id
    finally:
        delete_quietly(
            "knowledge base (unshared)",
            lambda: user_session_client.request("DELETE", f"{_KB_PATH}/{kb_id}"),
        )


def _demo_data_status(kb_client: KBClient) -> dict[str, Any]:
    return response_body(kb_client.get("/demo-data/status"), (200,), "Read demo data status")


@pytest.fixture(scope="module")
def contract_demo_data_preference(kb_client: KBClient) -> Iterator[str]:
    # Without a demo connector the API reports no choice, whatever is stored. Putting
    # back `null` then removes what the run stored.
    chosen = _demo_data_status(kb_client).get("chosen")
    try:
        yield json.dumps(chosen)
    finally:
        put_back = restore_quietly(
            "the demo data choice of the caller",
            lambda: kb_client.put("/demo-data/preference", json={"include": chosen}),
        )
        assert put_back, (
            "The contract run changed the demo data choice of the caller and could not put "
            f"it back. Set it by hand: PUT {_KB_PATH}/demo-data/preference "
            f"{json.dumps({'include': chosen})}"
        )


@pytest.fixture(scope="module")
def contract_demo_data_workspace(kb_client: KBClient) -> Iterator[bool]:
    """Whether the demo data is on for the organization. Every valid request writes this value.

    The teardown writes it once more, for the case that the API accepted another value
    against the spec.
    """
    enabled = _demo_data_status(kb_client).get("offForEveryone") is not True
    try:
        yield enabled
    finally:
        put_back = restore_quietly(
            "the demo data setting of the organization",
            lambda: kb_client.put("/demo-data/workspace", json={"enabled": enabled}),
        )
        assert put_back, (
            "The contract run could not put the demo data setting of the organization back. "
            f"Set it by hand: PUT {_KB_PATH}/demo-data/workspace "
            f"{json.dumps({'enabled': enabled})}"
        )


def _by_role(
    fixture: str, prefix: str, roles: tuple[str, ...], what: str, **kwargs: Any
) -> ValueSource:
    return ValueSource(
        fixture,
        tuple(f"{prefix}.{role}.id" for role in roles),
        lambda by_role: tuple(by_role[role] for role in roles),
        what,
        **kwargs,
    )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    _by_role(
        "contract_knowledge_bases",
        "knowledgeBase",
        KB_ROLES,
        "Four knowledge bases: one to read, one to update, one to delete, and one (`target`) "
        "that receives the folders and uploads of the create operations.",
    ),
    _by_role(
        "contract_folders",
        "folder",
        tuple(FOLDER_HOMES),
        "Six empty folders. Four are in the mutable knowledge base: to rename, to delete, to "
        "move a record into, and to reindex. `parent` is in the target knowledge base, for a "
        "subfolder or an upload. One more is in the readonly knowledge base, to be listed.",
    ),
    _by_role(
        "contract_records",
        "record",
        tuple(RECORD_HOMES),
        "Four uploaded one-line text files: one to read in the readonly knowledge base, and "
        "in the mutable one a record to rename and give a new file, one to delete and one to "
        "move. The upload starts the indexing of each file; the fixture does not wait for it.",
    ),
    ValueSource(
        "contract_text_file",
        ("file.text.path",),
        lambda path: (path,),
        "A one-line `.txt` file on the test machine; the fixture creates nothing on the "
        "deployment. uploadRecords uploads it, and updateRecord replaces the file of the "
        "mutable record with it.",
    ),
    ValueSource(
        "contract_grantee",
        ("user.grantee.id", "team.grantee.id"),
        lambda grantee: (grantee["user"], grantee["team"]),
        "A new user and a new team of the organization, to share knowledge bases with.",
    ),
    _by_role(
        "contract_shared_knowledge_bases",
        "knowledgeBase",
        SHARED_KB_ROLES,
        "Three knowledge bases that are shared with the grantee user (as READER) and team: "
        "one to list its permissions, one to change the role of the user, one to revoke both.",
    ),
    ValueSource(
        "contract_unshared_knowledge_base",
        ("knowledgeBase.unshared.id",),
        lambda kb_id: (kb_id,),
        "A knowledge base that the session user creates and shares with nobody, for "
        "createKBPermission to share.",
    ),
    ValueSource(
        "contract_demo_data_preference",
        ("demoData.preference.saved",),
        lambda saved: (saved,),
        "Creates nothing. Reads the caller's own demo data choice (`chosen`) before the run "
        "and writes it back when the tests of the suite are done.",
    ),
    ValueSource(
        "contract_demo_data_workspace",
        ("demoData.workspace.enabled",),
        lambda enabled: (enabled,),
        "Creates nothing. Reads whether the demo data is on for the organization. Every valid "
        "request of setDemoDataForEveryone writes that same value, and the fixture writes it "
        "once more after the suite. Each such write sets the sign-in of all sample accounts "
        "(`@acme-demo.example`) to the organization flag, also of one that an admin had set "
        "differently.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
