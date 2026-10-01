"""The contract suites together must cover the whole spec."""

from __future__ import annotations

from collections import Counter

import pytest

from helper.contract.runner import suite_paths
from helper.contract.spec import all_operations, load_spec
from helper.contract.suite import load_suite

pytestmark = pytest.mark.unit


def test_every_operation_of_the_spec_is_in_exactly_one_suite() -> None:
    """A new operation in the spec fails here until a suite decides how to test it."""
    in_suites = Counter(
        planned.operation.label
        for suite_path in suite_paths()
        for planned in load_suite(suite_path).operations
    )
    in_spec = {operation.label for operation in all_operations(load_spec())}

    assert sorted(in_spec - set(in_suites)) == [], "operations that no suite has"
    assert sorted(label for label, count in in_suites.items() if count > 1) == []
