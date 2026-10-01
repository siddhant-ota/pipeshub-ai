"""Mask credentials in the files of a run.

Schemathesis masks the `Authorization` header in its HAR file and nothing
else: not the header in its NDJSON report, not a body, not its log. A run logs
in as an admin, creates OAuth apps and access tokens, and the API answers with
their secrets. The files are kept and uploaded as build artifacts; this takes
the credentials out of them, before anything else reads them.

Two passes. The first reads the reports as data and masks the text under
every field with a credential name. The second works on plain text, for what
the first cannot reach: a response value that a check message quotes, the log,
a file that is cut off.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

logger = logging.getLogger("contract")

MASK = "[masked]"
_NAMES = (
    r"secret|password|passwd|token|api_?key|access_?key|account_?key|private_?key|"
    r"credentials?|connection_?string|code_?verifier|device_?code|authorization|cookie"
)
# `clientSecret`, `client_secret`, `accessToken`, `refresh_token`, `x-session-token`, ...
# Only a text under such a name is masked: `token: {id: ...}` is an object and stays.
_SENSITIVE_NAME = re.compile(rf"(?:{_NAMES})$", re.IGNORECASE)
# `"clientSecret": "abc`, also inside a JSON string (`\"clientSecret\": \"abc`) and cut off.
_QUOTED_FIELD = re.compile(
    rf'(\\*"[\w.-]*?(?:{_NAMES})\\*"\s*:\s*\\*")((?:[^"\\\n]|\\[^"\n])*)', re.IGNORECASE
)
# `client_secret=abc` in a form body, a query string or a curl command.
_FORM_FIELD = re.compile(rf"(\b[\w.-]*?(?:{_NAMES})=)([^&\s'\"\\]+)", re.IGNORECASE)
# A JWT (session and OAuth access tokens), and the access tokens PipesHub issues.
_TOKEN = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*|ph(?:pat|svc)_[A-Za-z0-9_-]{8,}"
)
_BASE64 = "$base64"


def _is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name))


def _mask_json(node: Any) -> Any:
    if isinstance(node, dict):
        # A header or query parameter as HAR writes it: {"name": ..., "value": ...}.
        if isinstance(node.get("name"), str) and isinstance(node.get("value"), str):
            if _is_sensitive(node["name"]) and node["value"]:
                return {**node, "value": MASK}
        return {
            key: MASK
            if isinstance(value, str) and value and _is_sensitive(str(key))
            else _mask_json(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_mask_json(item) for item in node]
    return node


def mask_text(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with the credentials masked that can be found without parsing it.

    `secrets` are values that are known to be credentials, wherever they stand.
    """
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    text = _QUOTED_FIELD.sub(lambda match: match[1] + MASK, text)
    text = _FORM_FIELD.sub(lambda match: match[1] + MASK, text)
    return _TOKEN.sub(MASK, text)


def mask_body(text: str) -> str:
    """A request or response body with the values of its credential fields masked."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict | list):
        masked = _mask_json(document)
        return text if masked == document else json.dumps(masked)
    # A form body: `grant_type=client_credentials&client_secret=...`.
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False) if "=" in text else []
    if pairs and any(_is_sensitive(name) and value for name, value in pairs):
        return urlencode(
            [(name, MASK if _is_sensitive(name) and value else value) for name, value in pairs]
        )
    return text


def _mask_encoded(holder: Any) -> None:
    """Mask a body that a report holds as `{"$base64": ...}`: no text pass can see into it."""
    if not isinstance(holder, dict) or not isinstance(holder.get(_BASE64), str):
        return
    try:
        text = base64.b64decode(holder[_BASE64]).decode("utf-8")
    except ValueError:
        return
    masked = _TOKEN.sub(MASK, mask_body(text))
    if masked != text:
        holder[_BASE64] = base64.b64encode(masked.encode("utf-8")).decode("ascii")


def _mask_all_encoded(node: Any) -> None:
    if isinstance(node, dict):
        _mask_encoded(node)
        for value in node.values():
            _mask_all_encoded(value)
    elif isinstance(node, list):
        for item in node:
            _mask_all_encoded(item)


def _mask_document(document: Any) -> Any:
    _mask_all_encoded(document)
    return _mask_json(document)


def _mask_har(document: Any) -> Any:
    for entry in (document.get("log") or {}).get("entries") or []:
        for holder in (
            (entry.get("request") or {}).get("postData"),
            (entry.get("response") or {}).get("content"),
        ):
            if not isinstance(holder, dict) or not isinstance(holder.get("text"), str):
                continue
            if holder.get("encoding") == "base64":
                encoded = {_BASE64: holder["text"]}
                _mask_encoded(encoded)
                holder["text"] = encoded[_BASE64]
            else:
                holder["text"] = mask_body(holder["text"])
    return _mask_json(document)


def redact_ndjson(path: Path, secrets: set[str]) -> None:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.dumps(_mask_document(json.loads(line)))
        except ValueError:
            # A line that was cut off: its encoded bodies cannot be reached, so it goes.
            logger.warning(
                "Dropped a line of %s that is not JSON, to keep it free of secrets", path
            )
            continue
        lines.append(mask_text(line, secrets))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def redact_har(path: Path, secrets: set[str]) -> None:
    try:
        text = json.dumps(_mask_har(json.loads(path.read_text(encoding="utf-8"))))
    except ValueError:
        logger.warning("Removed %s: it is not JSON, so it cannot be kept free of secrets", path)
        path.unlink()
        return
    path.write_text(mask_text(text, secrets), encoding="utf-8")


def redact_log(path: Path, secrets: set[str]) -> None:
    path.write_text(mask_text(path.read_text(encoding="utf-8"), secrets), encoding="utf-8")


def redact_run_files(
    events: list[Path], hars: list[Path], logs: list[Path], secrets: set[str]
) -> None:
    """Mask the credentials in the NDJSON reports, HAR files and logs of a run.

    `secrets` are the values that the fixtures marked as credentials. A file
    that does not exist, because the run did not get that far, is skipped.
    """
    for redact, paths in ((redact_ndjson, events), (redact_har, hars), (redact_log, logs)):
        for path in paths:
            if path.exists():
                redact(path, secrets)
