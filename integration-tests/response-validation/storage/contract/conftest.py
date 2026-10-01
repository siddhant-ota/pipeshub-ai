"""Fixtures for the storage contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`document.readonly.id`, `storage.token`, ...).

The internal storage routes refuse every user login. They take a service token
signed with the scoped JWT secret of the deployment, so the fixtures need
SCOPED_JWT_SECRET. Without it they skip, and all 11 operations of the suite are
skipped with that reason. With a secret that the deployment does not accept they
fail, and the operations fail with them. The token and the requests are those of
the storage integration tests (`integration-tests/storage`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.pipeshub_client import PipeshubClient

# `integration-tests/storage` has no `__init__.py`; with `integration-tests` on the path it
# imports as a namespace package.
from storage.storage_client import StorageClient, mint_storage_token, scoped_jwt_secret

SUITE_PATH = Path(__file__).with_name("suite.yaml")

# role -> how many versions the uploaded text file has; 0 for a document that keeps no versions.
# `readonly` has two, so that `version=0` and `version=1` both exist. `rollback` has three: the
# API rolls back only to a version older than the latest one, and the requests name 0 and 1.
UPLOADED_ROLES = {"readonly": 2, "mutable": 0, "versioned": 1, "rollback": 3}
# Document records without a file.
PLACEHOLDER_ROLES = ("placeholder", "disposable")
DOCUMENT_ROLES = (*UPLOADED_ROLES, *PLACEHOLDER_ROLES)
# The API accepts a new version only from a file with the extension of the first one.
_EXTENSION = "txt"
# A well-formed document ID that names nothing.
_NO_DOCUMENT = "0" * 24


def _document_id(resp: requests.Response, what: str) -> str:
    document = response_body(resp, (200,), what)
    assert document.get("_id"), f"{what}: response has no _id"
    return str(document["_id"])


@pytest.fixture(scope="module")
def contract_storage_client(pipeshub_client: PipeshubClient) -> StorageClient:
    """The client of the storage integration tests. It makes a fresh token for each request."""
    if not scoped_jwt_secret():
        pytest.skip(
            "SCOPED_JWT_SECRET is not set. The internal storage routes accept only a token "
            "signed with the scoped JWT secret of the deployment."
        )
    client = StorageClient(pipeshub_client)
    # With another secret every request is answered with 401, which the spec lists: an
    # operation whose requests all fail would pass, checked against nothing.
    assert client.get_document(_NO_DOCUMENT).status_code != 401, (
        "SCOPED_JWT_SECRET is not the scoped JWT secret of this deployment: the storage routes "
        "answer 401 to a token signed with it"
    )
    return client


@pytest.fixture(scope="module")
def contract_storage_token(
    pipeshub_client: PipeshubClient, contract_storage_client: StorageClient
) -> str:
    del contract_storage_client  # skips without the secret, fails with a wrong one
    return mint_storage_token(pipeshub_client.org_id, pipeshub_client.acting_user_id)


@pytest.fixture(scope="module")
def contract_text_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("contract-storage") / f"contract-upload.{_EXTENSION}"
    path.write_text("contract upload\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def contract_storage_documents(
    contract_storage_client: StorageClient,
) -> Iterator[dict[str, str]]:
    storage = contract_storage_client
    folder = f"contract-{uuid4().hex[:8]}"
    created: dict[str, str] = {}
    try:
        for role, versions in UPLOADED_ROLES.items():
            file_name = f"contract-{role}.{_EXTENSION}"
            resp = storage.upload(
                f"contract {role} v0".encode(),
                file_name,
                f"contract-{role}-{uuid4().hex[:8]}",
                is_versioned=versions > 0,
                document_path=folder,
            )
            created[role] = _document_id(resp, f"Upload document ({role})")
            for version in range(1, versions):
                resp = storage.upload_next_version(
                    created[role], f"contract {role} v{version}".encode(), file_name
                )
                _document_id(resp, f"Upload version {version} ({role})")
            stored = len(resp.json().get("versionHistory") or [])
            assert versions < 2 or stored >= versions, (
                f"Document ({role}): {stored} version(s), not {versions}"
            )
        for role in PLACEHOLDER_ROLES:
            resp = storage.create_placeholder(
                f"contract-{role}-{uuid4().hex[:8]}", _EXTENSION, folder
            )
            created[role] = _document_id(resp, f"Create placeholder ({role})")
        yield created
    finally:
        for role, document_id in created.items():
            delete_quietly(
                f"document ({role})",
                lambda document_id=document_id: storage.delete_document(document_id),
            )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_storage_token",
        ("storage.token",),
        lambda token: (token,),
        "A storage service token (scope `storage:token`, valid for one hour) for the organization "
        "and the user of the OAuth client, signed with SCOPED_JWT_SECRET. It creates nothing. "
        "Without the secret it skips, and all 11 operations with it; a secret that the "
        "deployment answers with 401 makes it fail.",
        secret=True,
    ),
    ValueSource(
        "contract_storage_documents",
        tuple(f"document.{role}.id" for role in DOCUMENT_ROLES),
        lambda documents: tuple(documents[role] for role in DOCUMENT_ROLES),
        "Six documents in one folder `contract-<8 hex chars>`. Four are uploaded text files: one "
        "with two versions to read, one to get new bytes, one to get a new version, and one with "
        "three versions to roll back. Two are records without a file: one to get an upload link, "
        "one to delete. At the end all six are marked deleted. The API removes nothing: the "
        "six records stay in MongoDB, and the files of the four uploaded documents, with every "
        "version, stay in the storage of the deployment (local, S3 or Azure Blob).",
    ),
    ValueSource(
        "contract_text_file",
        ("file.text.path",),
        lambda path: (str(path),),
        "A small text file `contract-upload.txt` in a temporary folder of pytest, sent as the "
        "file of the upload operations. It creates nothing on the deployment.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
