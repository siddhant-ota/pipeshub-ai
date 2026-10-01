"""Schemathesis hooks for the contract tests. Schemathesis loads this file through
the `hooks` key of its config, in the process that `runner.py` starts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import schemathesis

# `helper.*` is a namespace package rooted at integration-tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from helper.contract.config import (
    STATIC_AUTHORIZATION_ENV,
    SUBSTITUTIONS_ENV,
)
from helper.contract.values import (
    BODY,
    Mutation,
    Substitution,
    is_under_test,
    substitute,
)

_NEGATIVE = "negative"


def _load_substitutions() -> dict[str, list[Substitution]]:
    path = os.getenv(SUBSTITUTIONS_ENV)
    if not path:
        return {}
    raw: dict[str, dict[str, str]] = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        label: [Substitution.for_field(name, value) for name, value in fields.items()]
        for label, fields in raw.items()
    }


_SUBSTITUTIONS = _load_substitutions()
_client: Any = None


def _authorization() -> str:
    static = os.getenv(STATIC_AUTHORIZATION_ENV)
    if static:
        return static
    global _client
    if _client is None:
        from helper.pipeshub_client import PipeshubClient

        _client = PipeshubClient()
    return _client.auth_headers["Authorization"]


@schemathesis.auth(refresh_interval=60)
class IntegrationClientAuth:
    """The same OAuth client as the integration-test fixtures, so the caller owns the fixture data."""

    def get(self, case: schemathesis.Case, ctx: schemathesis.AuthContext) -> str:
        return _authorization()

    def set(self, case: schemathesis.Case, data: str, ctx: schemathesis.AuthContext) -> None:
        case.headers["Authorization"] = data


def _name(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _mutation(case: schemathesis.Case, location: str) -> Mutation | None:
    """What the case made invalid in `location`, or None if that part is valid."""
    meta = case.meta
    if meta is None:
        return None
    component = next(
        (info for where, info in meta.components.items() if _name(where) == location), None
    )
    if component is None or _name(component.mode) != _NEGATIVE:
        return None
    data = meta.phase.data
    return Mutation(
        location=_name(getattr(data, "parameter_location", None)) or location,
        parameter=str(getattr(data, "parameter", None) or ""),
        schema_pointer=str(getattr(data, "location", None) or ""),
    )


@schemathesis.hook
def before_call(ctx: schemathesis.HookContext, case: schemathesis.Case, **kwargs: Any) -> None:
    substitutions = _SUBSTITUTIONS.get(case.operation.label, ())
    # Read before anything is replaced: Schemathesis looks at a changed request again, and may
    # then describe it differently.
    mutations = {
        substitution.location: _mutation(case, substitution.location)
        for substitution in substitutions
    }
    for substitution in substitutions:
        if is_under_test(substitution, mutations[substitution.location]):
            continue
        attribute = "body" if substitution.location == BODY else "query"
        replaced, count = substitute(getattr(case, attribute), substitution)
        if count:
            setattr(case, attribute, replaced)
