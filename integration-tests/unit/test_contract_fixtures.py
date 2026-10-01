"""Every value a contract suite names must come from a fixture, and its baseline must fit it.

A key that no fixture provides, or a baseline that the suite cannot load, shows
only when the suite runs against a deployment. These find it without one.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from helper.contract.baseline import load_baseline
from helper.contract.pytest_support import TEST_MODULE_GLOB
from helper.contract.report import render_overview
from helper.contract.runner import BASELINE_NAME, SUITES_ROOT, suite_paths
from helper.contract.sources import fixture_rows, load_value_sources
from helper.contract.suite import PROFILE_FULL, load_suite

pytestmark = pytest.mark.unit

SUITES = suite_paths()


def _suite_id(suite_path: Path) -> str:
    return str(suite_path.parent.relative_to(SUITES_ROOT))


@pytest.mark.parametrize("suite_path", SUITES, ids=_suite_id)
def test_fixtures_provide_every_value_the_suite_names(suite_path: Path) -> None:
    suite = load_suite(suite_path)
    provided = Counter(key for source in load_value_sources(suite_path) for key in source.keys)

    assert not suite.fixture_keys - set(provided), (
        "suite.yaml names values that no fixture provides"
    )
    assert not [key for key, count in provided.items() if count > 1], "two fixtures provide one key"
    assert not set(provided) & set(suite.constants), "a fixture and a constant provide one key"


@pytest.mark.parametrize("suite_path", SUITES, ids=_suite_id)
def test_the_report_can_say_what_every_fixture_is(suite_path: Path) -> None:
    rows = fixture_rows(load_suite(suite_path), load_value_sources(suite_path))

    assert not [row.fixture for row in rows if len(row.what) < 10], (
        "a fixture without a description"
    )
    assert not [row.fixture for row in rows if not row.operations], "a fixture no operation uses"


@pytest.mark.parametrize("suite_path", SUITES, ids=_suite_id)
def test_the_baseline_names_only_operations_of_its_suite(suite_path: Path) -> None:
    """An operation that was renamed or removed must not leave its entries behind."""
    suite = load_suite(suite_path)

    load_baseline(
        suite_path.with_name(BASELINE_NAME),
        {planned.operation.operation_id for planned in suite.operations},
    )


@pytest.mark.parametrize("suite_path", SUITES, ids=_suite_id)
def test_every_suite_has_its_test_module(suite_path: Path) -> None:
    """Named after the suite: pytest cannot import two test modules with one base name."""
    name = load_suite(suite_path).name.replace("-", "_")

    assert [path.name for path in suite_path.parent.glob(TEST_MODULE_GLOB)] == [
        f"integration_test_{name}_contract.py"
    ]
    assert suite_path.with_name(BASELINE_NAME).exists()


def test_the_overview_names_every_suite_with_its_limits_and_fixtures() -> None:
    entries = [
        (
            load_suite(suite_path),
            fixture_rows(load_suite(suite_path), load_value_sources(suite_path)),
            _suite_id(suite_path),
        )
        for suite_path in SUITES
    ]

    overview = render_overview(entries)

    for suite, rows, _ in entries:
        assert f"## {suite.name}" in overview
        assert all(f"`{row.fixture}`" in overview for row in rows)
        assert all(
            planned.reason in overview
            for planned in suite.operations
            if planned.profile != PROFILE_FULL
        )
