"""Every value a contract suite names must come from a fixture.

A key that no fixture provides makes its operations fail only when the suite
runs against a deployment. This finds it without one.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

from helper.contract.suite import load_suite

pytestmark = pytest.mark.unit

CONTRACT_DIRS = sorted(
    path.parent
    for path in (Path(__file__).resolve().parents[1] / "response-validation").glob(
        "**/contract/suite.yaml"
    )
)


def _conftest(directory: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"contract_conftest_{directory.parent.name}", directory / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` looks its module up in sys.modules while the module is still loading.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("directory", CONTRACT_DIRS, ids=lambda path: path.parent.name)
def test_fixtures_provide_every_value_the_suite_names(directory: Path) -> None:
    provided = Counter(key for source in _conftest(directory).VALUE_SOURCES for key in source.keys)
    needed = load_suite(directory / "suite.yaml").value_keys

    assert not needed - set(provided), "suite.yaml names values that no fixture provides"
    assert not [key for key, count in provided.items() if count > 1], "two fixtures provide one key"
