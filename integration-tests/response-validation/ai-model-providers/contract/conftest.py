"""Fixtures for the AI-model contract tests.

`VALUE_SOURCES` turns the fixtures into the values that `suite.yaml` names
(`aiModel.mutable.key`, `modelRoles.saved`, ...).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from helper.ai_models_setup import SeededAIModel, setup_test_llm_model
from helper.clients.ai_models_client import AIModelsClient
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

# One model per role, so that an update, a delete, a change of the default or a role
# assignment under test cannot disturb another operation, in whatever order they run.
PROMOTABLE = "promotable"
MODEL_ROLES = ("mutable", "disposable", PROMOTABLE, "assignable")
_MODEL_TYPE = "llm"
# How `setup_test_llm_model` begins its error when the environment has no provider credentials.
# Any other error of it means that a provider refused the model.
_NO_CREDENTIALS = "No LLM provider credentials found"

_AI_MODELS = "/api/v1/configurationManager/ai-models"
_MODEL_ROLES = f"{_AI_MODELS}/roles"
_PREPARE_MODEL = f"{_AI_MODELS}/prepare-model"
_DOWNLOAD_PROGRESS = f"{_AI_MODELS}/download-progress"
# Making a model the default runs a health check against its provider first.
_HEALTH_CHECK_TIMEOUT_SEC = 180
_DOWNLOAD_FAILS_WITHIN_SEC = 120


def _default_llm_key(ai_models_client: AIModelsClient) -> str:
    """The key of the default LLM of the organization, or "" if it has none."""
    models = response_body(
        ai_models_client.get_models_by_type(_MODEL_TYPE), (200,), "List the LLMs"
    ).get("models")
    return next(
        (
            str(model["modelKey"])
            for model in models or []
            if isinstance(model, dict) and model.get("isDefault") and model.get("modelKey")
        ),
        "",
    )


def _is_default_llm(ai_models_client: AIModelsClient, model_key: str) -> bool:
    try:
        return _default_llm_key(ai_models_client) == model_key
    except (AssertionError, requests.RequestException, ValueError):
        return False


def _default_llm_is_back(
    pipeshub_client: PipeshubClient, ai_models_client: AIModelsClient, model_key: str
) -> bool:
    """Make `model_key` the default LLM again. True only if the API then lists it as the default.

    The API runs a provider health check on the model first, and changes nothing if it fails.
    """
    for _ in range(2):
        if _is_default_llm(ai_models_client, model_key):
            return True
        restore_quietly(
            "default LLM",
            lambda: pipeshub_client.request(
                "PUT",
                f"{_AI_MODELS}/default/{_MODEL_TYPE}/{model_key}",
                timeout=_HEALTH_CHECK_TIMEOUT_SEC,
            ),
        )
    return _is_default_llm(ai_models_client, model_key)


@pytest.fixture(scope="module")
def contract_ai_models(
    pipeshub_client: PipeshubClient, ai_models_client: AIModelsClient
) -> Iterator[dict[str, SeededAIModel]]:
    """One LLM per role. Skips without provider credentials; fails if a provider refuses.

    The teardown fails if the LLM that was the default before is not the default again.
    If no LLM was the default before, there is nothing to give back.
    """
    default_before = _default_llm_key(ai_models_client)
    created: dict[str, SeededAIModel] = {}
    try:
        for role in MODEL_ROLES:
            try:
                created[role] = setup_test_llm_model(pipeshub_client, is_default=False)
            except RuntimeError as exc:
                if str(exc).startswith(_NO_CREDENTIALS):
                    pytest.skip(str(exc))
                raise
        yield created
    finally:
        # setDefaultAIModel makes `promotable` the default LLM. The API deletes a default model
        # by promoting the first LLM that is left, which need not be the one that was the
        # default. So `promotable` is deleted only once the default is back where it was.
        default_is_back = (
            not default_before
            or not created
            or _default_llm_is_back(pipeshub_client, ai_models_client, default_before)
        )
        for role, model in created.items():
            if default_is_back or role != PROMOTABLE:
                delete_quietly(
                    f"LLM ({role})",
                    lambda model=model: ai_models_client.delete_provider(
                        model.model_type, model.model_key
                    ),
                )
        if not default_is_back:
            raise AssertionError(
                f"The default LLM is not back. Before this suite it was the model {default_before}; "
                "the API did not make it the default again (its provider health check must pass). "
                f"The fixture model `{PROMOTABLE}` was not deleted and may still be the default: "
                f"{created[PROMOTABLE].model_key if PROMOTABLE in created else 'not created'}."
            )


def _roles_are(user_session_client: SessionClient, roles: dict[str, object]) -> bool:
    try:
        resp = user_session_client.request("GET", _MODEL_ROLES)
        return resp.status_code == 200 and resp.json().get("modelRoles") == roles
    except (requests.RequestException, ValueError, AttributeError):
        return False


@pytest.fixture(scope="module")
def contract_model_roles(
    user_session_client: SessionClient, contract_ai_models: dict[str, SeededAIModel]
) -> Iterator[str]:
    """The role assignments as they are, as JSON. Puts them back at the end."""
    # After the models, for two reasons. updateModelRoles gives a role to one of them, and the
    # roles must be put back while that model still exists: deleting a model removes its roles.
    # And PUT /ai-models/roles creates the AI model configuration when there is none, with
    # `modelRoles` as its first entry; updateAIModelProvider and setDefaultAIModel iterate over
    # every entry up to the model they look for, and answer 500 from then on.
    del contract_ai_models
    saved = response_body(
        user_session_client.request("GET", _MODEL_ROLES), (200,), "Get the model roles"
    ).get("modelRoles")
    assert isinstance(saved, dict), "Get the model roles: response has no modelRoles object"

    saved_json = json.dumps(saved, sort_keys=True)
    try:
        yield saved_json
    finally:
        if not _roles_are(user_session_client, saved):
            restore_quietly(
                "model roles",
                lambda: user_session_client.request("PUT", _MODEL_ROLES, json={"roles": saved}),
            )
            assert _roles_are(user_session_client, saved), (
                f"The model roles are not back. Before this suite they were {saved_json}; "
                "the API did not take them again, or did not answer."
            )


def _wait_until_download_failed(pipeshub_client: PipeshubClient, model: str) -> None:
    deadline = time.monotonic() + _DOWNLOAD_FAILS_WITHIN_SEC
    with pipeshub_client.request(
        "GET", _DOWNLOAD_PROGRESS, params={"model": model}, stream=True
    ) as resp:
        assert resp.status_code == 200, (
            f"Download progress of {model}: HTTP {resp.status_code} {resp.text[:300]}"
        )
        for line in resp.iter_lines():
            if line.startswith(b"data:") and json.loads(line[5:]).get("status") == "failed":
                return
            assert time.monotonic() < deadline, (
                f"The download of {model} did not fail within {_DOWNLOAD_FAILS_WITHIN_SEC} s"
            )
    raise AssertionError(f"Download progress of {model}: the stream ended without `failed`")


@pytest.fixture(scope="module")
def contract_failed_embedding_download(pipeshub_client: PipeshubClient) -> str:
    """The name of an embedding model whose download has failed on the embedding server.

    The embedding server keeps the status of a download in memory until it restarts, and has
    no API to remove it, so there is nothing to delete.
    """
    # No such model is on the Hugging Face Hub, so the download fails at the first lookup.
    model = f"contract-failed-{uuid4().hex[:8]}"
    resp = pipeshub_client.request("POST", _PREPARE_MODEL, json={"model": model})
    # 403: the embedding server has a list of allowed models (EMBEDDING_SERVER_ALLOWED_MODELS).
    # 500: the Node API could not reach the embedding server.
    if resp.status_code in (403, 500):
        pytest.skip(
            "The embedding server of this deployment does not start a download for a model "
            f"name of the test: HTTP {resp.status_code} {resp.text[:200]}"
        )
    response_body(resp, (202,), f"Prepare the embedding model {model}")
    _wait_until_download_failed(pipeshub_client, model)
    return model


# unit/test_contract_fixtures.py checks that these keys cover every key suite.yaml uses.
VALUE_SOURCES: tuple[ValueSource, ...] = (
    ValueSource(
        "contract_ai_models",
        tuple(f"aiModel.{role}.key" for role in MODEL_ROLES),
        lambda models: tuple(models[role].model_key for role in MODEL_ROLES),
        "Four LLMs that are not the default, added with the provider credentials of the test "
        "environment: one for the update requests, one to delete, one to make the default LLM "
        "(the fixture gives the default back at the end), and one to give a role to.",
    ),
    ValueSource(
        "contract_model_roles",
        ("modelRoles.saved",),
        lambda saved: (saved,),
        "The model role assignments of the organization as they were before the run; it creates "
        "nothing, and writes them back at the end if they changed.",
    ),
    ValueSource(
        "contract_failed_embedding_download",
        ("embeddingModel.failed.name",),
        lambda model: (model,),
        "The name of an embedding model that does not exist on the Hugging Face Hub, for which "
        "the fixture started a download that failed, so that its progress stream ends at once.",
    ),
)

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
