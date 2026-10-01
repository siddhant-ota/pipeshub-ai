"""Schemathesis hooks for the contract tests. Schemathesis loads this file through
the `hooks` key of its config, in the process that `runner.py` starts."""

from __future__ import annotations

import base64
import itertools
from collections import defaultdict
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

import schemathesis
from schemathesis.transport import SerializationContext
from schemathesis.transport.prepare import prepare_body
from schemathesis.transport.requests import multipart_serializer
from schemathesis.transport.serialization import Binary

# `helper.*` is a namespace package rooted at integration-tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from helper.contract.config import (
    BASE_URL_ENV,
    FILES_ENV,
    HEADERS_ENV,
    LIMITED_ENV,
    LOGINS_ENV,
    STATIC_AUTHORIZATION_ENV,
    SUBSTITUTIONS_ENV,
    TOKENS_ENV,
)
from helper.contract.suite import AUTH_NONE, AUTH_OAUTH_CLIENT, AUTH_SESSION, AUTH_TOKEN
from helper.contract.fields import ANY_ITEM
from helper.contract.values import (
    BODY,
    PATH,
    Mutation,
    Substitution,
    is_under_test,
    substitute,
)

_NEGATIVE = "negative"
_UNKNOWN_PROPERTY = "object_unexpected_properties"
_MULTIPART = "multipart/"
# Log in again this long before the session token expires.
_EXPIRY_MARGIN_SEC = 120
# How long a session token is used when it does not say when it expires.
_DEFAULT_LIFETIME_SEC = 600


def _from_env(name: str) -> Any:
    return json.loads(os.getenv(name) or "{}")


_SUBSTITUTIONS: dict[str, list[Substitution]] = {
    label: [Substitution.for_field(name, value) for name, value in fields.items()]
    for label, fields in _from_env(SUBSTITUTIONS_ENV).items()
}
# Operation label -> form field of its multipart body -> path of the file to send in it.
_FILES: dict[str, dict[str, str]] = _from_env(FILES_ENV)
# Operation label -> header name -> value.
_HEADERS: dict[str, dict[str, str]] = _from_env(HEADERS_ENV)
_LOGINS: dict[str, str] = _from_env(LOGINS_ENV)
_TOKENS: dict[str, str] = _from_env(TOKENS_ENV)
_LIMITED: frozenset[str] = frozenset(_from_env(LIMITED_ENV) or ())
# The number of the request, for values that must be different in each one.
_case_numbers = itertools.count(1)
# Operation label -> how many requests it has had, to take the next value from a pool.
_turns: dict[str, itertools.count[int]] = defaultdict(itertools.count)


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


def _real_part(part: Any, path: Path) -> Any:
    """A file part of a multipart body, with the name, content and type of the file at `path`.

    `part` is what Schemathesis made of the generated value: `(file name, content)` with an
    optional content type, where the file name is that of the form field.
    """
    if not isinstance(part, tuple) or len(part) < 2 or part[0] is None:
        return part
    content_type = part[2] if len(part) > 2 else mimetypes.guess_type(path.name)[0]
    real = (path.name, path.read_bytes())
    return (*real, content_type) if content_type else real


def _as_sent(value: Any) -> Any:
    """A form value as a client writes it. Schemathesis sends a boolean as `True`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _form_part(part: Any, real_file: Path | None) -> Any:
    """One part of a multipart body, as Schemathesis built it, corrected for the wire."""
    if not isinstance(part, tuple) or len(part) < 2:
        return _as_sent(part)
    if part[0] is None:
        return (None, _as_sent(part[1]), *part[2:])
    return _real_part(part, real_file) if real_file else part


def _send_form_parts(
    case: schemathesis.Case, kwargs: dict[str, Any], mutation: Mutation | None
) -> None:
    """Build the parts of a multipart body here, to correct two things in them.

    Schemathesis generates an empty file and names it after its form field. An API that looks
    at the extension or the content rejects that, so no generated upload could succeed: a file
    field gets the real file that the suite gives for it. And Schemathesis writes a boolean
    field as `True`, which is the Python word and not what an API reads as a boolean.
    """
    if not isinstance(case.body, dict) or not (case.media_type or "").startswith(_MULTIPART):
        return
    body = prepare_body(case)
    if not isinstance(body, dict):
        return
    # On a copy: the serializer changes the body it is given, and Schemathesis serializes the
    # body of the case again when it sends the request.
    parts = multipart_serializer(SerializationContext(case=case), dict(body)).get("files")
    if not parts:
        return
    # A plan has placeholders for the values of fixtures; those name no file.
    files = {
        name: Path(path)
        for name, path in _FILES.get(case.operation.label, {}).items()
        if Path(path).is_file()
        and not is_under_test(Substitution(BODY, (name,), ""), mutation)
        and not is_under_test(Substitution(BODY, (name, ANY_ITEM), ""), mutation)
    }
    kwargs["files"] = [(name, _form_part(part, files.get(name))) for name, part in parts]


def _is_valid_in_disguise(case: schemathesis.Case) -> bool:
    """True for a request that is invalid only because it has a property the spec does not know.

    PipesHub does not reject an unknown property; it drops it and goes on. To the API such a
    request is a valid one. For an operation whose valid requests are limited, because they
    call an LLM or send an email, that is exactly the request the suite must not send.
    """
    meta = case.meta
    return (
        case.operation.label in _LIMITED
        and meta is not None
        and _name(getattr(meta.phase.data, "scenario", None)) == _UNKNOWN_PROPERTY
    )


def _holds_a_file(value: Any) -> bool:
    if isinstance(value, bytes | Binary):
        return True
    if isinstance(value, dict):
        return any(_holds_a_file(item) for item in value.values())
    return isinstance(value, list) and any(_holds_a_file(item) for item in value)


def _keep_the_label_of_a_file_body(case: schemathesis.Case) -> None:
    """Keep the valid/invalid label of a body that holds a file, after a value went into it.

    When a hook changed a body, Schemathesis validates it again, and it calls every body with
    bytes in it invalid without a look at the schema. A valid upload with a value in one of
    its text fields would then count as an invalid request that the API accepted. So the
    mark that the body was changed is taken off again.
    """
    meta = case._meta  # noqa: SLF001
    if meta is None or not _holds_a_file(case.body):
        return
    for location in meta.components:
        if _name(location) == BODY:
            meta.clear_dirty(location)


@schemathesis.hook
def before_call(
    ctx: schemathesis.HookContext, case: schemathesis.Case, kwargs: dict[str, Any]
) -> None:
    # A redirect is the answer to check: the spec documents the 302. Following it would judge
    # the response of another page, and send a request to wherever a generated URL points.
    kwargs.setdefault("allow_redirects", False)
    label = case.operation.label
    for name, value in _HEADERS.get(label, {}).items():
        case.headers[name] = value
    if _is_valid_in_disguise(case):
        # Sent without a login, so that the API turns it away and does nothing.
        case.headers.pop("Authorization", None)
    number, turn = next(_case_numbers), next(_turns[label])
    substitutions = [
        substitution.for_case(number, turn) for substitution in _SUBSTITUTIONS.get(label, ())
    ]
    # Read before anything is replaced: Schemathesis looks at a changed request again, and may
    # then describe it differently.
    mutations = {
        substitution.location: _mutation(case, substitution.location)
        for substitution in substitutions
    }
    body_mutation = _mutation(case, BODY)
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
            if substitution.location == BODY:
                _keep_the_label_of_a_file_body(case)
    # After the values: the parts are built from the body, with every field of the form.
    _send_form_parts(case, kwargs, body_mutation)
