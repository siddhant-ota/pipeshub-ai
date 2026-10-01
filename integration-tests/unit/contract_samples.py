"""A small OpenAPI document and Schemathesis events for the contract-helper unit tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import yaml

from helper.contract.config import STATE_FULL, OperationRun

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "paths": {
        "/things": {
            "get": {
                "operationId": "listThings",
                "x-pipeshub-sdk": True,
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}},
                    {"name": "projectId", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
            },
            "post": {
                "operationId": "createThing",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/NewThing"}}
                    }
                },
                "responses": {"201": {"content": {"application/json": {"schema": {}}}}},
            },
        },
        "/things/{thingId}": {
            "delete": {
                "operationId": "deleteThing",
                "parameters": [
                    {
                        "name": "thingId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"content": {"text/event-stream": {"schema": {}}}}},
            }
        },
        "/other": {"get": {"operationId": "outOfScope", "responses": {"200": {}}}},
    },
    "components": {
        "schemas": {
            "NewThing": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ownerId": {"type": "string"},
                    "filters": {
                        "allOf": [{"$ref": "#/components/schemas/Filters"}],
                        "description": "Scope by ids.",
                    },
                    "models": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "modelKey": {"type": "string"},
                                "provider": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "Filters": {
                "type": "object",
                "properties": {
                    "kb": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Each element must be a valid UUID.",
                    },
                    "sort": {"type": "string", "enum": ["asc", "desc"]},
                },
            },
        }
    },
}

SUITE: dict[str, Any] = {
    "name": "things",
    "include_path_regex": "^/things",
    "path_parameters": {
        "defaults": [{"path_prefix": "/things", "values": {"thingId": "thing.id"}}]
    },
    "values": {
        "query.projectId": "project.id",
        "body.filters.kb[*]": "kb.id",
        "body.models[*].modelKey": "llm.key",
    },
    "waived_id_fields": [{"field": "body.ownerId", "reason": "Any string is accepted."}],
}


def write_suite(directory: Path, **changes: Any) -> Path:
    path = directory / "suite.yaml"
    path.write_text(yaml.safe_dump({**SUITE, **changes}), encoding="utf-8")
    return path


def operation_run(operation_id: str, method: str, path: str, **changes: Any) -> OperationRun:
    fields = {"sdk": False, "state": STATE_FULL, **changes}
    return OperationRun(operation_id=operation_id, method=method, path=path, **fields)


def _encoded(value: Any) -> dict[str, str]:
    text = value if isinstance(value, str) else json.dumps(value)
    return {"$base64": base64.b64encode(text.encode()).decode()}


def case_event(
    label: str,
    *,
    case_id: str,
    status: int = 200,
    mode: str = "positive",
    query: str = "",
    body: Any = None,
    response: Any = None,
    data: dict[str, Any] | None = None,
    failed: dict[str, str] | None = None,
    passed: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One `ScenarioFinished` event with a single case, shaped like Schemathesis 4 writes it.

    `failed` maps a check name to its failure message.
    """
    method, path = label.split(" ", 1)
    request: dict[str, Any] = {
        "method": method,
        "uri": f"http://localhost/api/v1{path}{'?' + query if query else ''}",
    }
    if body is not None:
        request["body"] = _encoded(body)
    checks = [{"name": name, "status": "success"} for name in passed]
    checks += [
        {
            "name": name,
            "status": "failure",
            "failure_info": {"failure": {"title": f"{name} failed", "message": message}},
        }
        for name, message in (failed or {}).items()
    ]
    return {
        "ScenarioFinished": {
            "phase": "coverage",
            "recorder": {
                "label": label,
                "cases": {
                    case_id: {
                        "value": {
                            "method": method,
                            "meta": {"generation": {"mode": mode}, "phase": {"data": data or {}}},
                        }
                    }
                },
                "checks": {case_id: checks},
                "interactions": {
                    case_id: {
                        "request": request,
                        "response": {
                            "status_code": status,
                            "content": _encoded(response if response is not None else {}),
                        },
                    }
                },
            },
        }
    }


def write_events(directory: Path, events: list[dict[str, Any]]) -> Path:
    path = directory / "events.ndjson"
    lines = [json.dumps({"Initialize": {}}), *(json.dumps(event) for event in events)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
