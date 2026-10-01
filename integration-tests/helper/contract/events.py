"""Read the test cases and check results from a Schemathesis NDJSON report."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

API_PREFIX = "/api/v1"
NEGATIVE = "negative"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    title: str = ""
    message: str = ""


@dataclass(frozen=True)
class Case:
    case_id: str
    # "<METHOD> <path template>" of the spec operation under test
    label: str
    phase: str
    mode: str
    # Where the case differs from a plain valid request: `query`, `body`, ... or "".
    location: str
    # Query: the parameter name. Body: the media type.
    parameter: str
    # Body: where in the schema the case acts, as a JSON Schema pointer.
    schema_pointer: str
    scenario: str
    description: str
    method: str
    target: str
    status: int | None
    checks: tuple[Check, ...]
    request_base64: str = ""
    response_base64: str = ""

    @property
    def is_negative(self) -> bool:
        return self.mode == NEGATIVE

    @property
    def request_body(self) -> str:
        return base64.b64decode(self.request_base64).decode("utf-8", errors="replace")

    def request_json(self) -> Any:
        """The request body as JSON, or None if it is empty or not JSON."""
        return _json(self.request_base64)

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.target).query, keep_blank_values=True)

    def response_json(self) -> Any:
        """The response body as JSON, or None if it is empty or not JSON."""
        return _json(self.response_base64)

    @property
    def what(self) -> str:
        """One line that says what this case sends."""
        description = (
            self.description.removeprefix(f"{self.parameter}: ")
            if self.parameter
            else self.description
        )
        where = " ".join(part for part in (self.location, self.parameter) if part)
        return f"{where}: {description}" if where else description


def _json(encoded: str) -> Any:
    if not encoded:
        return None
    try:
        return json.loads(base64.b64decode(encoded))
    except ValueError:
        return None


def _target(uri: str) -> str:
    parts = urlsplit(uri)
    path = parts.path.removeprefix(API_PREFIX)
    return f"{path}?{parts.query}" if parts.query else path


def _base64(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    return value.get("$base64") or "" if isinstance(value, dict) else ""


def _checks(raw: list[dict[str, Any]] | None) -> tuple[Check, ...]:
    checks: list[Check] = []
    for entry in raw or []:
        failure = (entry.get("failure_info") or {}).get("failure") or {}
        checks.append(
            Check(
                name=entry.get("name", ""),
                passed=entry.get("status") == "success",
                title=failure.get("title", ""),
                message=failure.get("message", ""),
            )
        )
    return tuple(checks)


def read_cases(ndjson_path: Path) -> Iterator[Case]:
    with open(ndjson_path, encoding="utf-8") as handle:
        for line in handle:
            scenario = json.loads(line).get("ScenarioFinished")
            if not scenario:
                continue
            recorder = scenario.get("recorder") or {}
            interactions = recorder.get("interactions") or {}
            checks = recorder.get("checks") or {}
            for case_id, entry in (recorder.get("cases") or {}).items():
                value = entry.get("value") or {}
                meta = value.get("meta") or {}
                data = (meta.get("phase") or {}).get("data") or {}
                interaction = interactions.get(case_id) or {}
                request = interaction.get("request") or {}
                response = interaction.get("response") or {}
                yield Case(
                    case_id=case_id,
                    label=recorder.get("label", ""),
                    phase=scenario.get("phase", ""),
                    mode=(meta.get("generation") or {}).get("mode") or "",
                    location=data.get("parameter_location") or "",
                    parameter=data.get("parameter") or "",
                    schema_pointer=data.get("location") or "",
                    scenario=data.get("scenario") or "",
                    description=data.get("description") or "",
                    method=value.get("method", ""),
                    target=_target(request.get("uri", "")),
                    status=response.get("status_code"),
                    checks=_checks(checks.get(case_id)),
                    request_base64=_base64(request, "body"),
                    response_base64=_base64(response, "content"),
                )
