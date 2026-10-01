"""Fixtures for the notifications contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`notification.archivable.id`, ...).

No route creates a notification: the API writes one when a background job ends.
The only job a test can start without an outside system is the bulk invite from
an uploaded file, which tells its caller the result. A file whose only address
is malformed invites nobody and sends no mail, and still ends in a notification.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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

NOTIFICATIONS = "/api/v1/notifications"
# `archivable` is there to be archived; `archived` already is, to be unarchived.
ARCHIVED = "archived"
NOTIFICATION_ROLES = ("markRead", "markUnread", "archivable", ARCHIVED, "disposable")
_PAGE_SIZE = 50
_MAX_PAGES = 20
# The notification is written by a broker consumer, some time after the upload is answered.
_NOTIFICATION_WAIT_SEC = 60
_POLL_SEC = 0.5
# What `smtpConfigCheck` answers while the deployment has no SMTP settings.
_NO_SMTP_SETTINGS = (404, 500)
# Keeps the restore under the global rate limiter, whatever the run sent just before.
_RESTORE_PAUSE_SEC = 0.1


def _unread_page(client: SessionClient, cursor: str | None = None) -> dict[str, Any]:
    params: dict[str, str | int] = {"status": "unread", "limit": _PAGE_SIZE}
    if cursor:
        params["cursor"] = cursor
    return response_body(
        client.request("GET", NOTIFICATIONS, params=params), (200,), "List unread notifications"
    )


@pytest.fixture(scope="module")
def contract_unread_notifications(user_session_client: SessionClient) -> Iterator[list[str]]:
    unread: list[str] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        page = _unread_page(user_session_client, cursor)
        unread.extend(str(item["_id"]) for item in page.get("notifications") or [])
        cursor = page.get("cursor")
        if not (page.get("hasMore") and cursor):
            break
    else:
        pytest.fail(
            f"The test user has more than {_PAGE_SIZE * _MAX_PAGES} unread notifications; "
            "that is more than this fixture saves and marks unread again"
        )
    try:
        yield unread
    finally:
        for notification_id in unread:
            restore_quietly(
                f"unread notification {notification_id}",
                lambda notification_id=notification_id: user_session_client.request(
                    "PATCH", f"{NOTIFICATIONS}/{notification_id}/unread"
                ),
            )
            time.sleep(_RESTORE_PAUSE_SEC)


def _notification_about(client: SessionClient, address: str) -> str:
    """The ID of the notification that reports `address` as invalid, once it is there."""
    deadline = time.monotonic() + _NOTIFICATION_WAIT_SEC
    while True:
        for item in _unread_page(client).get("notifications") or []:
            if address in ((item.get("payload") or {}).get("invalid") or []):
                return str(item["_id"])
        assert time.monotonic() < deadline, (
            f"No notification about the bulk invite of {address} within {_NOTIFICATION_WAIT_SEC} s; "
            "is the notification consumer of the API running?"
        )
        time.sleep(_POLL_SEC)


@pytest.fixture(scope="module")
def contract_notifications(
    request: pytest.FixtureRequest, user_session_client: SessionClient
) -> Iterator[dict[str, str]]:
    users = UsersClient(user_session_client)
    created: dict[str, str] = {}
    try:
        for role in NOTIFICATION_ROLES:
            # No dot after the `@`: the API takes it for an address, finds it invalid and
            # invites nobody. It reports addresses in lower case.
            address = f"contract-{role}-{uuid4().hex[:8]}@invalid".lower()
            csv = f"Email\n{address}\n".encode()
            resp = users.invite_bulk_upload(csv, "invites.csv")
            if not created and resp.status_code in _NO_SMTP_SETTINGS:
                try:
                    request.getfixturevalue("smtp_configured")
                except pytest.skip.Exception as exc:
                    pytest.skip(
                        "The bulk-invite upload, the only way to get a notification, is refused "
                        f"until the deployment has SMTP settings (HTTP {resp.status_code}): {exc}"
                    )
                resp = users.invite_bulk_upload(csv, "invites.csv")
            response_body(resp, (202,), f"Bulk invite upload ({role})")
            created[role] = _notification_about(user_session_client, address)
        response_body(
            user_session_client.request("PATCH", f"{NOTIFICATIONS}/{created[ARCHIVED]}/archive"),
            (200,),
            "Archive notification",
        )
        yield created
    finally:
        for role, notification_id in created.items():
            delete_quietly(
                f"notification ({role})",
                lambda notification_id=notification_id: user_session_client.request(
                    "DELETE", f"{NOTIFICATIONS}/{notification_id}"
                ),
            )


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# The unread notifications are saved first, before the second fixture adds unread ones.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_unread_notifications",
        ("notification.unreadBefore.count",),
        lambda unread: (str(len(unread)),),
        "How many notifications of the test user were unread before the run. It creates nothing; "
        "at the end it marks those notifications unread again, because "
        "`PATCH /notifications/read-all` marks them all read.",
    ),
    ValueSource(
        "contract_notifications",
        tuple(f"notification.{role}.id" for role in NOTIFICATION_ROLES),
        lambda notifications: tuple(notifications[role] for role in NOTIFICATION_ROLES),
        "Five notifications of the test user: to mark read, to mark unread, to archive, already "
        "archived (to unarchive), and to dismiss. Each one comes from one bulk-invite upload "
        "(`POST /users/bulk/invite/upload`) of a CSV file with one malformed address, which "
        "invites nobody and sends no mail. At the end all five are dismissed.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
