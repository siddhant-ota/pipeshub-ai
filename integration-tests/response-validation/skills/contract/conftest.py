"""Fixtures for the skills contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`skill.mutable.name`, `skill.versioned.version`, ...).

The API shows a user only the skills that this user created, and the suite logs
in with the session of the test user. So the fixtures create every skill with
`user_session_client`, not with the OAuth client.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from helper.contract.pytest_support import delete_quietly, response_body, suite_fixtures
from helper.contract.sources import ValueSource
from helper.contract.values import CASE_NUMBER
from helper.http.session_client import SessionClient

logger = logging.getLogger("contract")

SUITE_PATH = Path(__file__).with_name("suite.yaml")
SKILLS = "/api/v1/skills"

# One skill per role, so that an update, a status change or a delete under test
# cannot change what another operation reads, in whatever order they run.
# `enabled` is there to be disabled; `disabled` already is, to be enabled.
PLAIN_ROLES = (
    "readonly",
    "mutable",
    "patchable",
    "deprecatable",
    "enabled",
    "resourceWritable",
)
# One skill for each request of `deleteSkill` in the plan. A request can delete its skill
# without being a valid one (see `deleteSkill` in suite.yaml), and the next one needs another.
DISPOSABLE_ROLES = tuple(f"disposable{number}" for number in range(1, 8))
VERSIONED_ROLES = ("versioned", "rollbackable")
RESOURCE_ROLES = ("resourceReadable", "resourceRemovable")
DISABLED = "disabled"

# In the body of every fixture skill exactly once; see `patchSkillBody` in suite.yaml.
PATCH_MARKER = "contract-patch-marker"
RESOURCE_PATH = "references/contract.md"


def _name(role: str) -> str:
    # The API takes lowercase letters, digits and single hyphens only (skills/validator.py).
    return f"contract-{role.lower()}-{uuid4().hex[:8]}"


def _content(role: str) -> dict[str, str]:
    return {
        "description": f"Contract-test skill ({role}).",
        "body": f"A skill of the API contract tests. {PATCH_MARKER}",
    }


def _skill_md(name: str) -> str:
    """A SKILL.md file, as an archive has it and the finalize operation takes it."""
    return (
        f"---\nname: {name}\ndescription: Contract-test skill (imported).\n---\n\n"
        "A skill of the API contract tests.\n"
    )


def _delete(client: SessionClient, what: str, name: str) -> None:
    delete_quietly(what, lambda: client.request("DELETE", f"{SKILLS}/{name}"))


def _delete_named(client: SessionClient, prefixes: tuple[str, ...]) -> None:
    """Delete the skills of the test user whose name starts with one of `prefixes`."""
    try:
        resp = client.request("GET", SKILLS)
        skills = resp.json().get("skills") if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001 - a teardown must not stop the ones after it
        logger.warning("Could not list the skills to delete: %s", exc)
        return
    if not isinstance(skills, list):
        logger.warning(
            "Could not list the skills to delete: HTTP %s %s", resp.status_code, resp.text[:200]
        )
        return
    for skill in skills:
        name = skill.get("name") if isinstance(skill, dict) else None
        if isinstance(name, str) and name.startswith(prefixes):
            _delete(client, f"skill {name}", name)


@contextmanager
def _skills(client: SessionClient, roles: tuple[str, ...]) -> Iterator[dict[str, str]]:
    """Create one skill per role, role -> name, and delete them at the end."""
    names: dict[str, str] = {}
    try:
        for role in roles:
            names[role] = _name(role)
            resp = client.request("POST", SKILLS, json={"name": names[role], **_content(role)})
            response_body(resp, (201,), f"Create skill ({role})")
        yield names
    finally:
        for role, name in names.items():
            _delete(client, f"skill ({role})", name)


@pytest.fixture(scope="module")
def contract_skills(user_session_client: SessionClient) -> Iterator[dict[str, str]]:
    with _skills(user_session_client, PLAIN_ROLES) as names:
        yield names


@pytest.fixture(scope="module")
def contract_disposable_skills(user_session_client: SessionClient) -> Iterator[list[str]]:
    with _skills(user_session_client, DISPOSABLE_ROLES) as names:
        yield list(names.values())


@pytest.fixture(scope="module")
def contract_versioned_skills(
    user_session_client: SessionClient,
) -> Iterator[dict[str, dict[str, str]]]:
    """Skills with one archived version: an update archives the version it replaces."""
    client = user_session_client
    with _skills(client, VERSIONED_ROLES) as names:
        versioned: dict[str, dict[str, str]] = {}
        for role, name in names.items():
            response_body(
                client.request("PUT", f"{SKILLS}/{name}", json=_content(role)),
                (200,),
                f"Update skill ({role})",
            )
            listed = response_body(
                client.request("GET", f"{SKILLS}/{name}/versions"),
                (200,),
                f"List versions ({role})",
            )
            versions = listed.get("versions") or []
            assert versions and versions[0].get("version"), (
                f"List versions ({role}): the update archived no version"
            )
            versioned[role] = {"name": name, "version": str(versions[0]["version"])}
        yield versioned


@pytest.fixture(scope="module")
def contract_skills_with_resource(
    user_session_client: SessionClient,
) -> Iterator[dict[str, str]]:
    client = user_session_client
    with _skills(client, RESOURCE_ROLES) as names:
        for role, name in names.items():
            response_body(
                client.request(
                    "PUT",
                    f"{SKILLS}/{name}/resource",
                    json={
                        "path": RESOURCE_PATH,
                        "content": "A resource of a contract-test skill.\n",
                    },
                ),
                (200,),
                f"Write resource ({role})",
            )
        yield names


@pytest.fixture(scope="module")
def contract_disabled_skill(user_session_client: SessionClient) -> Iterator[str]:
    client = user_session_client
    with _skills(client, (DISABLED,)) as names:
        response_body(
            client.request("POST", f"{SKILLS}/{names[DISABLED]}/disable"),
            (200,),
            "Disable skill",
        )
        yield names[DISABLED]


@pytest.fixture(scope="module")
def contract_new_skill_names(user_session_client: SessionClient) -> Iterator[dict[str, str]]:
    """The names of the skills that the create and the import operation under test make.

    Nothing is created here. Each request gets its own name: the prefix of this run and
    the number of the request. `created_resources` deletes the skills that a 2xx response
    names. The import writes the bundled resources after it created the skill, so it can
    answer with an error for a skill that now exists; the teardown finds those by the prefix.
    """
    prefixes = {role: f"{_name(role)}-" for role in ("created", "imported")}
    try:
        yield {role: f"{prefix}{CASE_NUMBER}" for role, prefix in prefixes.items()}
    finally:
        _delete_named(user_session_client, tuple(prefixes.values()))


@pytest.fixture(scope="module")
def contract_skill_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("contract-skills") / "contract-skill.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("SKILL.md", _skill_md("contract-uploaded"))
        archive.writestr(RESOURCE_PATH, "A resource of a contract-test skill.\n")
    return path


# The API calls the embedding model of the deployment once for each new skill (see suite.yaml).
_EMBEDS = "Calls to the embedding model: {}."

# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_skills",
        (*(f"skill.{role}.name" for role in PLAIN_ROLES), "skill.patchable.marker"),
        lambda names: (*(names[role] for role in PLAIN_ROLES), PATCH_MARKER),
        "Six skills of the test user: to read, to update, to patch the body of, to deprecate, "
        "to disable, and to write a bundled resource to. The marker is a text that the body of "
        f"each has exactly once. {_EMBEDS.format(6)}",
    ),
    ValueSource(
        "contract_disposable_skills",
        ("skill.disposable.name",),
        lambda names: (names,),
        "Seven skills of the test user to delete, one for each request of the delete operation. "
        f"{_EMBEDS.format(7)}",
    ),
    ValueSource(
        "contract_versioned_skills",
        tuple(f"skill.{role}.{what}" for role in VERSIONED_ROLES for what in ("name", "version")),
        lambda skills: tuple(
            skills[role][what] for role in VERSIONED_ROLES for what in ("name", "version")
        ),
        "Two skills that were updated once, so each has one archived version: one to read the "
        f"versions of, and one to roll back. {_EMBEDS.format(2)}",
    ),
    ValueSource(
        "contract_skills_with_resource",
        tuple(f"skill.{role}.{what}" for role in RESOURCE_ROLES for what in ("name", "path")),
        lambda names: tuple(
            value for role in RESOURCE_ROLES for value in (names[role], RESOURCE_PATH)
        ),
        "Two skills with one bundled resource each, at the given path: one to read the resource "
        f"of, and one to remove it from. {_EMBEDS.format(2)}",
    ),
    ValueSource(
        "contract_disabled_skill",
        ("skill.disabled.name",),
        lambda name: (name,),
        f"One skill that is already disabled, for the enable operation. {_EMBEDS.format(1)}",
    ),
    ValueSource(
        "contract_new_skill_names",
        ("skill.created.name", "skill.imported.name", "skill.imported.content"),
        lambda names: (names["created"], names["imported"], _skill_md(names["imported"])),
        "Creates nothing. A name for each skill that the create operation makes, and a name and "
        "the SKILL.md text for each skill that the import makes; `{case}` is the number of the "
        "request. At the end it deletes the skills of the test user that still have these names. "
        "The operations create up to 43 skills (30 and 13), and the API calls the embedding model "
        "once for each.",
    ),
    ValueSource(
        "contract_skill_archive",
        ("skill.archive.path",),
        lambda path: (str(path),),
        "Creates nothing on the deployment. A zip file in pytest's temporary folder with a "
        "SKILL.md and one resource file, for the upload preview.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
