"""Fixtures for the user-management contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`user.mutable.id`, `team.readonly.id`, ...).
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests

from helper.clients.auth_client import UserAccountClient
from helper.clients.org_client import OrgClient
from helper.clients.teams_client import TeamsClient
from helper.clients.user_groups_client import UserGroupsClient
from helper.clients.users_client import UsersClient
from helper.contract.pytest_support import (
    delete_quietly,
    response_body,
    restore_quietly,
    suite_fixtures,
)
from helper.contract.sources import ValueSource
from helper.http.protocol import HTTPClientProtocol
from helper.http.session_client import SessionClient

SUITE_PATH = Path(__file__).with_name("suite.yaml")
NOTIFICATIONS = "/api/v1/notifications"

logger = logging.getLogger("contract")

# One resource per role, so that an update or a delete under test cannot change
# what another operation reads, in whatever order they run. `member` is the user
# that the membership operations add and remove: the `joinable` group does not
# have it, the `leavable` group does.
USER_ROLES = ("readonly", "mutable", "disposable", "member")
GROUP_ROLES = ("readonly", "mutable", "disposable", "joinable", "leavable")
TEAM_ROLES = ("readonly", "mutable", "disposable")

# Reserved (RFC 2606): no mail to a fixture user can reach anyone.
_EMAIL_DOMAIN = "example.com"
# createUser takes a starting password only for an address of this domain
# (users.controller.ts), and only a user with a password can get one wrong.
_DEMO_EMAIL_DOMAIN = "acme-demo.example"
# The API blocks a login at the fifth wrong password (userAccount.controller.ts).
_WRONG_LOGINS_TO_BLOCK = 5
# Login requests that the run itself sends right after the fixtures.
_LOGINS_LEFT_FOR_THE_RUN = 4
_GRAPH_WAIT_SEC = 60
_GRAPH_POLL_SEC = 2
# The API gives at most 50 notifications in one page, newest first.
_NOTIFICATION_PAGE_SIZE = 50
_NOTIFICATION_PAGES = 4
_NOTIFICATION_QUIET_SEC = 6
_NOTIFICATION_POLL_SEC = 2
_NOTIFICATION_WAIT_SEC = 60

_ORG_TEXT_FIELDS = ("registeredName", "shortName", "contactEmail")
_ADDRESS_FIELDS = ("addressLine1", "city", "state", "postCode", "country")

# A red pixel. The API takes it for a display picture and for a logo.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _name(role: str) -> str:
    return f"contract-{role}-{uuid4().hex[:8]}"


def _create_user(
    users_client: UsersClient, role: str, domain: str, **fields: str
) -> dict[str, str]:
    name = _name(role)
    email = f"{name}@{domain}"
    resp = users_client.create_user(email=email, full_name=name, **fields)
    user = response_body(resp, (201,), f"Create user ({role})")
    assert user.get("_id"), f"Create user ({role}): response has no _id"
    return {"id": str(user["_id"]), "email": email}


@pytest.fixture(scope="module")
def contract_users(users_client: UsersClient) -> Iterator[dict[str, dict[str, str]]]:
    created: dict[str, dict[str, str]] = {}
    try:
        for role in USER_ROLES:
            created[role] = _create_user(users_client, role, _EMAIL_DOMAIN)
        # The API sends no invitation to a user who has logged in. The readonly user is the
        # `{id}` of every operation that names no other, also of resendUserInvite, and its
        # address is the only one that bulkInviteUsers gets.
        updated = response_body(
            users_client.update_user(created["readonly"]["id"], hasLoggedIn=True),
            (200,),
            "Mark the readonly user as logged in",
        )
        assert updated.get("hasLoggedIn") is True, "The readonly user is not marked as logged in"
        yield created
    finally:
        # The API refuses to delete an admin, and updateUser can have made the mutable user one.
        if "mutable" in created:
            restore_quietly(
                "the member role of user (mutable)",
                lambda: users_client.update_user(created["mutable"]["id"], role="member"),
            )
        for role, user in created.items():
            delete_quietly(f"user ({role})", lambda user=user: users_client.delete_user(user["id"]))


@pytest.fixture(scope="module")
def contract_new_names() -> dict[str, str]:
    """Names for what the test cases create. The API refuses an email or a group name twice.

    `{case}` becomes the number of the request (helper/contract/values.py).
    """
    return {
        "email": f"{_name('new')}-{{case}}@{_EMAIL_DOMAIN}",
        "group": f"{_name('new')}-{{case}}",
        "renamed": f"{_name('renamed')}-{{case}}",
    }


@pytest.fixture(scope="module")
def contract_image_file(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("contract") / "contract-picture.png"
    path.write_bytes(_TINY_PNG)
    return str(path)


def _invite_notifications(client: HTTPClientProtocol, address: str) -> list[str]:
    """The IDs of the notifications of the caller that report `address` as invalid."""
    found: list[str] = []
    cursor = ""
    for _ in range(_NOTIFICATION_PAGES):
        params = {"limit": _NOTIFICATION_PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
        page = response_body(
            client.request("GET", NOTIFICATIONS, params=params), (200,), "List notifications"
        )
        found += [
            str(item["_id"])
            for item in page.get("notifications") or []
            if address in ((item.get("payload") or {}).get("invalid") or [])
        ]
        cursor = page.get("cursor") or ""
        if not (page.get("hasMore") and cursor):
            break
    return found


def _dismiss_invite_notifications(client: HTTPClientProtocol, address: str) -> None:
    """Dismiss the notifications about `address`.

    One arrives through the message broker for each upload that the API accepted, after
    the answer to the upload. So this goes on until none has come for a while.
    """
    deadline = time.monotonic() + _NOTIFICATION_WAIT_SEC
    quiet_since = time.monotonic()
    while (now := time.monotonic()) < deadline and now < quiet_since + _NOTIFICATION_QUIET_SEC:
        try:
            found = _invite_notifications(client, address)
        except Exception as exc:  # noqa: BLE001 - logged and not raised, as in delete_quietly
            logger.warning("Could not look for the notifications about %s: %s", address, exc)
            return
        for notification_id in found:
            delete_quietly(
                f"notification {notification_id}",
                lambda notification_id=notification_id: client.request(
                    "DELETE", f"{NOTIFICATIONS}/{notification_id}"
                ),
            )
        if found:
            quiet_since = time.monotonic()
        time.sleep(_NOTIFICATION_POLL_SEC)


@pytest.fixture(scope="module")
def contract_invite_file(
    pipeshub_client: HTTPClientProtocol,
    tmp_path_factory: pytest.TempPathFactory,
    smtp_configured: None,
) -> Iterator[str]:
    """A CSV file for bulkInviteUsersFromFile that invites nobody.

    Its one address has no dot after the `@`: the API takes it for an address, finds it
    invalid and invites nobody. Each upload that the API accepts still leaves the caller
    a notification, which names that address. The caller is the user that the OAuth client
    acts as, so the same client finds the notifications and dismisses them.
    """
    del smtp_configured  # without SMTP the API accepts no upload and leaves no notification
    address = f"{_name('invite')}@invalid"
    # Fails here, before any upload, if this login cannot read its notifications.
    _invite_notifications(pipeshub_client, address)
    path = tmp_path_factory.mktemp("contract") / "contract-invite.csv"
    path.write_text(f"Email\n{address}\n", encoding="utf-8")
    try:
        yield str(path)
    finally:
        _dismiss_invite_notifications(pipeshub_client, address)


def _in_graph(users_client: UsersClient, user: dict[str, str]) -> bool:
    resp = users_client.graph_list(search=user["email"].split("@", 1)[0], limit="50")
    if resp.status_code != 200:
        return False
    return any(str(entry.get("userId")) == user["id"] for entry in resp.json().get("users") or [])


@pytest.fixture(scope="module")
def contract_team_member(
    users_client: UsersClient, contract_users: dict[str, dict[str, str]]
) -> str:
    """The `member` user, once the graph has it.

    A new user reaches the graph through the message broker, after the create
    call returns. Until then the team operations answer 400 for it.
    """
    member = contract_users["member"]
    deadline = time.monotonic() + _GRAPH_WAIT_SEC
    while not _in_graph(users_client, member):
        assert time.monotonic() < deadline, (
            f"user {member['email']} did not reach the graph within {_GRAPH_WAIT_SEC} s"
        )
        time.sleep(_GRAPH_POLL_SEC)
    return member["id"]


def _seconds_to_next_window(resp: requests.Response) -> float:
    return float(resp.headers.get("RateLimit-Reset") or 60) + 1


def _paced(send: Callable[[], requests.Response], keep_free: int = 1) -> requests.Response:
    """Send one login request without using up the login rate limit.

    The API limits login requests for each client address (10 a minute unless
    MAX_AUTH_REQUESTS_PER_MINUTE says otherwise), and the run logs in from the
    same address. When fewer than `keep_free` requests are left in the current
    minute, this waits for the next one.
    """
    resp = send()
    if resp.status_code == 429:
        time.sleep(_seconds_to_next_window(resp))
        resp = send()
    remaining = resp.headers.get("RateLimit-Remaining")
    if remaining is not None and int(remaining) < keep_free:
        time.sleep(_seconds_to_next_window(resp))
    return resp


@pytest.fixture(scope="module")
def contract_blocked_user(
    users_client: UsersClient, user_account_client: UserAccountClient
) -> Iterator[str]:
    user: dict[str, str] = {}
    try:
        user = _create_user(
            users_client, "blocked", _DEMO_EMAIL_DOMAIN, password=f"Aa1!{uuid4().hex}"
        )
        email = user["email"]
        for attempt in range(1, _WRONG_LOGINS_TO_BLOCK + 1):
            started = _paced(lambda: user_account_client.init_auth(email))
            session_token = started.headers.get("x-session-token")
            assert started.status_code == 200 and session_token, (
                f"Start login {attempt} of the blocked user: "
                f"HTTP {started.status_code} {started.text[:300]}"
            )
            refused = _paced(
                lambda: user_account_client.authenticate(
                    session_token, email, f"wrong-{uuid4().hex}"
                ),
                keep_free=_LOGINS_LEFT_FOR_THE_RUN if attempt == _WRONG_LOGINS_TO_BLOCK else 1,
            )
            assert refused.status_code in (400, 401), (
                f"Wrong password {attempt} of the blocked user: "
                f"HTTP {refused.status_code} {refused.text[:300]}"
            )
        listed = response_body(
            users_client.get_all_users(isBlocked="true", search=email),
            (200,),
            "List the blocked users",
        )
        assert any(
            entry.get("id") == user["id"] and entry.get("isBlocked")
            for entry in listed.get("users") or []
        ), f"user {email} is not blocked after {_WRONG_LOGINS_TO_BLOCK} wrong passwords"
        yield user["id"]
    finally:
        if user:
            delete_quietly("user (blocked)", lambda: users_client.delete_user(user["id"]))


@pytest.fixture(scope="module")
def contract_user_groups(
    user_groups_client: UserGroupsClient, contract_users: dict[str, dict[str, str]]
) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in GROUP_ROLES:
            group = response_body(
                user_groups_client.create_group(_name(role)), (201,), f"Create user group ({role})"
            )
            assert group.get("_id"), f"Create user group ({role}): response has no _id"
            created[role] = str(group["_id"])
        for group_role, user_role in (("readonly", "readonly"), ("leavable", "member")):
            response_body(
                user_groups_client.add_users(
                    [contract_users[user_role]["id"]], [created[group_role]]
                ),
                (200,),
                f"Add the {user_role} user to the {group_role} group",
            )
        yield created
    finally:
        for role, group_id in created.items():
            delete_quietly(
                f"user group ({role})",
                lambda group_id=group_id: user_groups_client.delete_group(group_id),
            )


@pytest.fixture(scope="module")
def contract_teams(teams_client: TeamsClient) -> Iterator[dict[str, str]]:
    created: dict[str, str] = {}
    try:
        for role in TEAM_ROLES:
            resp = teams_client.create_team(_name(role))
            team = response_body(resp, (201,), f"Create team ({role})").get("data") or {}
            assert team.get("id"), f"Create team ({role}): response has no data.id"
            created[role] = str(team["id"])
        yield created
    finally:
        for role, team_id in created.items():
            delete_quietly(
                f"team ({role})", lambda team_id=team_id: teams_client.delete_team(team_id)
            )


def _image(resp: requests.Response, what: str) -> tuple[bytes, str] | None:
    """The image in a response and its type, or None if the API says there is none."""
    assert resp.status_code in (200, 204), f"{what}: HTTP {resp.status_code} {resp.text[:300]}"
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    return (resp.content, content_type) if content_type.startswith("image/") else None


def _keep_image(
    what: str,
    read: Callable[[], requests.Response],
    upload: Callable[..., requests.Response],
    remove: Callable[[], requests.Response],
) -> Iterator[str]:
    """Make sure there is an image during the run, and put back what was there before.

    The API stores a PNG or JPEG as a JPEG that it encodes itself, so an image
    that is put back is encoded once more.
    """
    saved = _image(read(), f"Read the {what}")
    try:
        if saved is None:
            resp = upload(_TINY_PNG)
            assert resp.status_code == 201, (
                f"Upload a {what}: HTTP {resp.status_code} {resp.text[:300]}"
            )
        yield "none" if saved is None else f"{saved[1]}, {len(saved[0])} bytes"
    finally:
        if saved is None:
            delete_quietly(what, remove)
        else:
            restore_quietly(what, lambda: upload(saved[0], content_type=saved[1]))


@pytest.fixture(scope="module")
def contract_display_picture(users_client: UsersClient) -> Iterator[str]:
    yield from _keep_image(
        "display picture",
        users_client.get_display_picture,
        users_client.upload_display_picture,
        users_client.remove_display_picture,
    )


@pytest.fixture(scope="module")
def contract_org_logo(org_client: OrgClient) -> Iterator[str]:
    yield from _keep_image(
        "organization logo", org_client.get_logo, org_client.upload_logo, org_client.remove_logo
    )


def _restore_org_profile(org_client: OrgClient, saved: dict[str, Any]) -> None:
    body: dict[str, Any] = {name: saved[name] for name in _ORG_TEXT_FIELDS if saved.get(name)}
    address = saved.get("permanentAddress") or {}
    if address:
        body["permanentAddress"] = {
            name: address[name] for name in _ADDRESS_FIELDS if name in address
        }
    # The API writes only values that are not empty (org.controller.ts), so it cannot remove a
    # field that a request of the run added. Such a field gets the value that shows like none:
    # the short name is shown in place of the registered name, an address of empty lines as none.
    try:
        now = response_body(org_client.get_organization(), (200,), "Read the organization")
    except Exception:  # noqa: BLE001 - what the organization had before is still put back
        now = {}
    if now.get("shortName") and "shortName" not in body and "registeredName" in body:
        body["shortName"] = body["registeredName"]
    if now.get("permanentAddress") and not address:
        body["permanentAddress"] = dict.fromkeys(_ADDRESS_FIELDS, "")
    # Twice: the event that renames the organization in the graph carries the name from before
    # the update (org.controller.ts), so only the second one carries the name that was put back.
    for _ in range(2):
        restore_quietly("organization profile", lambda: org_client.update_organization(**body))


@pytest.fixture(scope="module")
def contract_org_profile(org_client: OrgClient) -> Iterator[dict[str, Any]]:
    saved = response_body(org_client.get_organization(), (200,), "Read the organization")
    try:
        yield saved
    finally:
        _restore_org_profile(org_client, saved)


@pytest.fixture(scope="module")
def contract_onboarding_status(user_session_client: SessionClient) -> Iterator[str]:
    org_client = OrgClient(user_session_client)
    saved = response_body(
        org_client.get_onboarding_status(), (200,), "Read the onboarding status"
    ).get("status")
    assert saved, "Read the onboarding status: response has no status"
    try:
        yield str(saved)
    finally:
        restore_quietly(
            "onboarding status", lambda: org_client.update_onboarding_status(status=saved)
        )


def _acting_user(client: Any) -> tuple[str]:
    assert client.acting_user_id, "the access token of the tests names no user"
    return (client.acting_user_id,)


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
# The order is the order in which a run creates them. The fixtures that log in come last,
# and the one that uses up login requests after the one that needs a single login.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "pipeshub_client",
        ("user.self.id",),
        _acting_user,
        "The user the tests log in as, who creates the fixture teams. Nothing is created.",
        added=False,
    ),
    ValueSource(
        "contract_users",
        (
            *(f"user.{role}.id" for role in USER_ROLES),
            "user.readonly.email",
            "user.mutable.email",
        ),
        lambda users: (
            *(users[role]["id"] for role in USER_ROLES),
            users["readonly"]["email"],
            users["mutable"]["email"],
        ),
        "Four users at example.com: to read, to update, to delete, and one that the group and "
        "team operations add and remove as a member. The one to read is marked as logged in, "
        "so that the API sends it no invitation.",
    ),
    ValueSource(
        "contract_new_names",
        ("user.new.email", "userGroup.new.name", "userGroup.renamed.name"),
        lambda names: (names["email"], names["group"], names["renamed"]),
        "Names that nothing has yet, a different one in each request: the email for the users "
        "that createUser creates, and the names for the groups that createUserGroup creates "
        "and updateUserGroup renames. The fixture creates nothing.",
    ),
    ValueSource(
        "contract_image_file",
        ("image.path",),
        lambda path: (path,),
        "A PNG file of one red pixel in a temporary folder of the test run, to upload as a "
        "display picture and as a logo. Nothing is created on the deployment.",
    ),
    ValueSource(
        "contract_user_groups",
        tuple(f"userGroup.{role}.id" for role in GROUP_ROLES),
        lambda groups: tuple(groups[role] for role in GROUP_ROLES),
        "Five custom user groups: to read (with the readonly user in it), to rename, to "
        "delete, one to add the member user to, and one that has it, to remove it from.",
    ),
    ValueSource(
        "contract_teams",
        tuple(f"team.{role}.id" for role in TEAM_ROLES),
        lambda teams: tuple(teams[role] for role in TEAM_ROLES),
        "Three teams that the user the tests log in as owns: to read, to update, and to delete.",
    ),
    ValueSource(
        "contract_display_picture",
        ("user.self.displayPicture",),
        lambda saved: (saved,),
        "The display picture of the user the tests log in as, as it was before the run. "
        "If there is none, the fixture uploads a red pixel. At the end it puts the old "
        "picture back, or removes the one of the run.",
    ),
    ValueSource(
        "contract_org_logo",
        ("org.logo",),
        lambda saved: (saved,),
        "The logo of the organization, as it was before the run. If there is none, the "
        "fixture uploads a red pixel. At the end it puts the old logo back, or removes "
        "the one of the run.",
    ),
    ValueSource(
        "contract_org_profile",
        tuple(f"org.current.{name}" for name in _ORG_TEXT_FIELDS),
        lambda org: tuple(str(org.get(name) or "") for name in _ORG_TEXT_FIELDS),
        "The names and the contact email of the organization, as they were before the run. "
        "The fixture creates nothing and puts the profile, with the address, back at the end.",
    ),
    ValueSource(
        "contract_onboarding_status",
        ("org.onboardingStatus",),
        lambda status: (status,),
        "The onboarding status of the organization, as it was before the run. The fixture "
        "creates nothing and puts the status back at the end.",
    ),
    ValueSource(
        "smtp_configured",
        ("smtp.configured",),
        lambda _: ("configured",),
        "Changes the deployment and is not undone: it writes the SMTP server of the test "
        "environment (SMTP_HOST, SMTP_PORT) into the settings, and the API does not give the "
        "old password back. Skipped without those two variables.",
        added=False,
    ),
    ValueSource(
        "contract_invite_file",
        ("invite.path",),
        lambda path: (path,),
        "A CSV file in a temporary folder of the test run, with one address that the API "
        "finds invalid, so that an upload invites nobody. Each accepted upload leaves the "
        "user the tests log in as a notification; the fixture dismisses them at the end.",
    ),
    ValueSource(
        "contract_team_member",
        ("user.teamMember.id",),
        lambda user_id: (user_id,),
        "The member user again, once it has reached the graph, where teams look their "
        "members up. The fixture waits and creates nothing.",
    ),
    ValueSource(
        "contract_blocked_user",
        ("user.blocked.id",),
        lambda user_id: (user_id,),
        "A user at acme-demo.example with a password, whose login the fixture blocks with "
        "five wrong passwords, for the unblock operation.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
