"""Where the values of a suite come from: its fixtures, and what the report says about them."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from helper.contract.suite import PROFILE_SKIP, Suite
from helper.contract.values import ContractValues, ValueOrPool

# The fixtures of a suite are in the conftest.py next to its suite file.
CONFTEST_NAME = "conftest.py"
SECRET = "(secret)"
_CONSTANTS = "constants in suite.yaml"


@dataclass(frozen=True)
class ValueSource:
    """The value keys one fixture provides, and how to read them from it, in the same order."""

    fixture: str
    keys: tuple[str, ...]
    read: Callable[[Any], tuple[str, ...]]
    # One sentence for the report: what the fixture is and what it creates on the deployment.
    what: str
    # False for a fixture that the integration tests had before the contract tests.
    added: bool = True
    # True if the values are credentials. The report and the files of a run do not show them.
    secret: bool = False


@dataclass(frozen=True)
class FixtureRow:
    """What the report says about one fixture."""

    fixture: str
    keys: tuple[str, ...]
    what: str
    added: bool
    secret: bool = False
    # IDs of the operations that use one of its values.
    operations: tuple[str, ...] = ()
    # value key -> the value in this run; empty in a plan
    values: dict[str, ValueOrPool] = field(default_factory=dict)
    # Why the fixture gave no values in this run, or "".
    problem: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FixtureRow:
        return cls(**{**data, "keys": tuple(data["keys"]), "operations": tuple(data["operations"])})

    def with_values(self, values: ContractValues) -> FixtureRow:
        """The row with the value each key had in a run, or why it had none."""
        return replace(
            self,
            values={
                key: SECRET if self.secret else values.values[key]
                for key in self.keys
                if key in values.values
            },
            problem=next(
                (
                    reasons[key]
                    for reasons in (values.missing, values.unavailable)
                    for key in self.keys
                    if key in reasons
                ),
                "",
            ),
        )


def _users(suite: Suite, keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        planned.operation.operation_id
        for planned in suite.operations
        if planned.profile != PROFILE_SKIP and planned.value_keys & set(keys)
    )


def fixture_rows(suite: Suite, sources: tuple[ValueSource, ...]) -> list[FixtureRow]:
    rows = [
        FixtureRow(
            fixture=source.fixture,
            keys=source.keys,
            what=source.what,
            added=source.added,
            secret=source.secret,
            operations=_users(suite, source.keys),
        )
        for source in sources
    ]
    if suite.constants:
        keys = tuple(suite.constants)
        rows.append(
            FixtureRow(
                fixture=_CONSTANTS,
                keys=keys,
                what="Values that the suite file gives itself; nothing is created.",
                added=True,
                operations=_users(suite, keys),
            )
        )
    return rows


def load_value_sources(suite_path: Path) -> tuple[ValueSource, ...]:
    """`VALUE_SOURCES` of the conftest.py next to the suite file. Runs no fixture."""
    conftest = Path(suite_path).with_name(CONFTEST_NAME)
    if not conftest.exists():
        return ()
    name = f"contract_conftest_{abs(hash(str(conftest.resolve())))}"
    # The integration-test helpers import each other by bare name (`from pipeshub_client import
    # ...`); the root conftest puts their folder on the path, and so must this.
    helpers = str(Path(__file__).resolve().parents[1])
    if helpers not in sys.path:
        sys.path.insert(0, helpers)
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, conftest)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {conftest}")
        module = importlib.util.module_from_spec(spec)
        # `@dataclass` looks its module up in sys.modules while the module is still loading.
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return tuple(sys.modules[name].VALUE_SOURCES)
