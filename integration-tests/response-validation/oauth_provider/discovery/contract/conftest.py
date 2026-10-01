"""Fixtures for the OIDC discovery contract tests. The suite names no value, so it has none."""

from __future__ import annotations

from pathlib import Path

from helper.contract.pytest_support import suite_fixtures
from helper.contract.sources import ValueSource

SUITE_PATH = Path(__file__).with_name("suite.yaml")

VALUE_SOURCES: tuple[ValueSource, ...] = ()

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
