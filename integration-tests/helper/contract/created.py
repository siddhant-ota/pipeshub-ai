"""What the test cases of a run created on the deployment, so the test can delete it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from helper.contract.events import read_cases
from helper.contract.suite import Suite
from helper.contract.values import ContractValues


def _at_pointer(document: Any, pointer: str) -> Any:
    for part in pointer.strip("/").split("/"):
        if not isinstance(document, dict):
            return None
        document = document.get(part)
    return document


def created_resource_paths(suite: Suite, values: ContractValues, events_path: Path) -> list[str]:
    """API paths to DELETE, one for each resource a 2xx response reported as created.

    A path that still contains `{...}` names a value that the run did not have.
    """
    if not events_path.exists():
        return []
    rules = {rule.operation.label: rule for rule in suite.created_resources}
    paths: list[str] = []
    for case in read_cases(events_path):
        rule = rules.get(case.label)
        if rule is None or case.status is None or not 200 <= case.status < 300:
            continue
        resource_id = _at_pointer(case.response_json(), rule.id_pointer)
        if not isinstance(resource_id, str) or not resource_id:
            continue
        path = rule.delete_path.replace("{id}", resource_id)
        for key, value in values.values.items():
            path = path.replace(f"{{{key}}}", value)
        paths.append(path)
    return paths
