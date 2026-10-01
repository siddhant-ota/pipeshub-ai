"""Schemathesis hooks for the contract tests. Schemathesis loads this file through
the `hooks` key of its config, in the process that `runner.py` starts."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import schemathesis

# `helper.*` is a namespace package rooted at integration-tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from helper.contract.config import (
    BASE_URL_ENV,
    LOGINS_ENV,
    STATIC_AUTHORIZATION_ENV,
    SUBSTITUTIONS_ENV,
    TOKENS_ENV,
)
from helper.contract.suite import AUTH_NONE, AUTH_OAUTH_CLIENT, AUTH_SESSION, AUTH_TOKEN
from helper.contract.values import (
    BODY,
    PATH,
    Mutation,
    Substitution,
    is_under_test,
    substitute,
)

_NEGATIVE = "negative"
# Log in again this long before the session token expires.
_EXPIRY_MARGIN_SEC = 120
# How long a session token is used when it does not say when it expires.
_DEFAULT_LIFETIME_SEC = 600


def _from_env(name: str) -> dict[str, Any]:
    return json.loads(os.getenv(name) or "{}")


_SUBSTITUTIONS: dict[str, list[Substitution]] = {
    label: [Substitution.for_field(name, value) for name, value in fields.items()]
    for label, fields in _from_env(SUBSTITUTIONS_ENV).items()
}
_LOGINS: dict[str, str] = _from_env(LOGINS_ENV)
_TOKENS: dict[str, str] = _from_env(TOKENS_ENV)


def _expiry(token: str) -> float:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return time.time() + _DEFAULT_LIFETIME_SEC


class _Logins:
    """The Authorization header for each way to log in, made when it is first needed."""

    def __init__(self) -> None:
        self._client: Any = None
        self._session_token = ""
        self._session_expires = 0.0

    def _oauth_client(self) -> str:
        if self._client is None:
            from helper.pipeshub_client import PipeshubClient

            self._client = PipeshubClient(base_url=os.getenv(BASE_URL_ENV))
        return self._client.auth_headers["Authorization"]

    def _session(self) -> str:
        if time.time() > self._session_expires - _EXPIRY_MARGIN_SEC:
            from helper.local_auth import obtain_user_session_token

            self._session_token = obtain_user_session_token(os.getenv(BASE_URL_ENV, ""))
            self._session_expires = _expiry(self._session_token)
        return f"Bearer {self._session_token}"

    def header(self, label: str) -> str:
        """The Authorization header for the operation, or "" if it gets none."""
        login = _LOGINS.get(label, AUTH_OAUTH_CLIENT)
        if login == AUTH_NONE:
            return ""
        static = os.getenv(STATIC_AUTHORIZATION_ENV)
        if static:
            return static
        if login == AUTH_TOKEN:
            return f"Bearer {_TOKENS[label]}"
        return self._session() if login == AUTH_SESSION else self._oauth_client()


_logins = _Logins()


@schemathesis.auth()
class OperationAuth:
    """Each operation logs in the way the suite says; see `suite.load_suite`."""

    def get(self, case: schemathesis.Case, ctx: schemathesis.AuthContext) -> _Logins:
        return _logins

    def set(self, case: schemathesis.Case, data: _Logins, ctx: schemathesis.AuthContext) -> None:
        header = data.header(case.operation.label)
        if header:
            case.headers["Authorization"] = header


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
        if substitution.location == PATH:
            name = substitution.path[0]
            if name in (case.path_parameters or {}):
                case.path_parameters[name] = substitution.value
            continue
        attribute = "body" if substitution.location == BODY else "query"
        replaced, count = substitute(getattr(case, attribute), substitution)
        if count:
            setattr(case, attribute, replaced)
