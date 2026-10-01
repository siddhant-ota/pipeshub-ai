"""What the test cases of a run created on the deployment, so the test can delete it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helper.contract.events import read_cases
from helper.contract.suite import Suite
from helper.contract.values import ContractValues


@dataclass(frozen=True)
class Leftover:
    """One resource to delete: the API path, and how to log in for it."""

    path: str
    auth: str
    # AUTH_TOKEN: the value key of the token.
    token_key: str = ""


def _at_pointer(document: Any, pointer: str) -> Any:
    for part in pointer.strip("/").split("/"):
        if isinstance(document, list) and part.isdigit() and int(part) < len(document):
            document = document[int(part)]
        elif isinstance(document, dict):
            document = document.get(part)
        else:
            return None
    return document


def created_resources(suite: Suite, values: ContractValues, events_path: Path) -> list[Leftover]:
    """What to DELETE, one entry for each resource a 2xx response reported as created.

    A path that still contains `{...}` names a value that the run did not have.
    """
    if not events_path.exists():
        return []
    leftovers: list[Leftover] = []
    for case in read_cases(events_path):
        if case.status is None or not 200 <= case.status < 300:
            continue
        # One operation can have several rules: an upload answers with a list of new records.
        for rule in suite.created_resources:
            if rule.operation.label != case.label:
                continue
            resource_id = _at_pointer(case.response_json(), rule.id_pointer)
            if not isinstance(resource_id, str) or not resource_id:
                continue
            path = rule.delete_path.replace("{id}", resource_id)
            for key, value in values.values.items():
                path = path.replace(f"{{{key}}}", str(value))
            leftovers.append(Leftover(path, rule.auth, rule.token_key))
    return leftovers
