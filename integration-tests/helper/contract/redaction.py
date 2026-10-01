"""Mask credentials in the files of a run.

Schemathesis masks the `Authorization` header in its reports, and nothing in a
body. A run creates OAuth apps and access tokens, and the API answers with
their secrets, so the report files would hold credentials that work until the
run deletes what it created. The files are kept and uploaded as build
artifacts; this takes the credentials out of them.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

MASK = "[masked]"
# `clientSecret`, `client_secret`, `accessToken`, `refresh_token`, `password`, `apiKey`, ...
# Only a text under such a name is masked: `token: {id: ...}` is an object and stays.
_SENSITIVE_NAME = re.compile(
    r"(secret|password|passwd|token|api_?key|private_?key|credentials?)$", re.IGNORECASE
)
_BASE64 = "$base64"


def _is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name))


def _mask_json(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: MASK
            if isinstance(value, str) and value and _is_sensitive(str(key))
            else _mask_json(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_mask_json(item) for item in node]
    return node


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


def _mask_encoded(container: Any, key: str) -> None:
    """Mask a body that the NDJSON report holds as `{key: {"$base64": ...}}`."""
    holder = container.get(key) if isinstance(container, dict) else None
    if not isinstance(holder, dict) or not holder.get(_BASE64):
        return
    try:
        text = base64.b64decode(holder[_BASE64]).decode("utf-8")
    except ValueError:
        return
    masked = mask_body(text)
    if masked != text:
        holder[_BASE64] = base64.b64encode(masked.encode("utf-8")).decode("ascii")


def _mask_event(event: dict[str, Any]) -> None:
    recorder = (event.get("ScenarioFinished") or {}).get("recorder") or {}
    for interaction in (recorder.get("interactions") or {}).values():
        _mask_encoded(interaction.get("request"), "body")
        _mask_encoded(interaction.get("response"), "content")


def _mask_har_entry(entry: dict[str, Any]) -> None:
    for holder in (
        (entry.get("request") or {}).get("postData"),
        (entry.get("response") or {}).get("content"),
    ):
        if not isinstance(holder, dict) or not isinstance(holder.get("text"), str):
            continue
        if holder.get("encoding") == "base64":
            holder["$base64"] = holder["text"]
            _mask_encoded({"body": holder}, "body")
            holder["text"] = holder.pop("$base64")
        else:
            holder["text"] = mask_body(holder["text"])


def _without(text: str, secrets: set[str]) -> str:
    """`text` with the given values masked wherever they stand, for example in a URL."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    return text


def redact_ndjson(path: Path, secrets: set[str]) -> None:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        _mask_event(event)
        lines.append(_without(json.dumps(event), secrets))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def redact_har(path: Path, secrets: set[str]) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in (document.get("log") or {}).get("entries") or []:
        _mask_har_entry(entry)
    path.write_text(_without(json.dumps(document), secrets), encoding="utf-8")


def redact_run_files(files: tuple[Path, Path], secrets: set[str]) -> None:
    """Mask the credentials in the NDJSON report and the HAR file of a run.

    `secrets` are the values that the fixtures marked as credentials. A file
    that does not exist, because the run did not get that far, is skipped.
    """
    events, har = files
    if events.exists():
        redact_ndjson(events, secrets)
    if har.exists():
        redact_har(har, secrets)
