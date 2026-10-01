"""The baseline: differences between the spec and the API that are known and accepted for now.

A contract test fails for a difference that is not in the baseline, and for a
baseline entry that no longer occurs, so the file can only get shorter.

Schemathesis has its own `--baseline`. It is not used: it identifies a failure
by operation, check and failure class only, so one accepted "the API accepted
an invalid request" on an operation would hide every later one on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helper.contract.results import FindingKey, OperationResult

FORMAT_VERSION = 1
_KEY_FIELDS = ("operation", "check", "subject", "detail")


class BaselineError(ValueError):
    """The baseline file cannot be used as it is."""


def _entry_key(entry: dict[str, Any]) -> FindingKey:
    try:
        return FindingKey(
            entry["operation"], entry["check"], entry["subject"], entry.get("detail", "")
        )
    except (KeyError, TypeError) as exc:
        raise BaselineError(
            f"a baseline entry needs operation, check and subject: {entry!r}"
        ) from exc


def _read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("format_version")
    if version != FORMAT_VERSION:
        raise BaselineError(f"{path}: unsupported baseline format version {version!r}")
    return list(raw.get("findings") or [])


def load_baseline(path: Path, operation_ids: set[str]) -> set[FindingKey]:
    """The accepted differences. `operation_ids` are the operations of the suite.

    An entry for an operation the suite does not have can never be found again
    or go stale, so it would stay in the file for ever. It is an error instead.
    """
    baseline = {_entry_key(entry) for entry in _read_entries(path)}
    unknown = sorted({key.operation_id for key in baseline} - operation_ids)
    if unknown:
        raise BaselineError(
            f"{path} has entries for operations that are not in the suite: {', '.join(unknown)}"
        )
    return baseline


def write_baseline(path: Path, results: list[OperationResult]) -> tuple[int, int]:
    """Make the baseline say what the run found. Returns (entries added, entries removed).

    An entry is removed only if its operation ran to the end and the difference
    did not show. Entries of operations that were not sent, or not finished,
    stay as they are, and so does anything a person added to an entry (a note,
    a ticket).
    """
    existing = {_entry_key(entry): entry for entry in _read_entries(path)}
    ran_to_the_end = {
        result.run.operation_id
        for result in results
        if result.run.is_sent and result.cases and not result.unfinished
    }
    found = {key for result in results for key in result.findings}

    kept = {
        key: entry
        for key, entry in existing.items()
        if key in found or key.operation_id not in ran_to_the_end
    }
    added = found - set(existing)
    entries = [
        *kept.values(),
        *(
            {
                "operation": key.operation_id,
                "check": key.check,
                "subject": key.subject,
                "detail": key.detail,
            }
            for key in added
        ),
    ]
    entries.sort(key=lambda entry: tuple(str(entry.get(name, "")) for name in _KEY_FIELDS))
    path.write_text(
        json.dumps({"format_version": FORMAT_VERSION, "findings": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(added), len(existing) - len(kept)
