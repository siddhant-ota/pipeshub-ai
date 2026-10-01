"""Fixtures for the MCP contract tests.

`POST /mcp` and `GET /mcp` take no ID and need no data, so no fixture gives a value.
"""

from __future__ import annotations

from pathlib import Path

from helper.contract.pytest_support import suite_fixtures
from helper.contract.sources import ValueSource

SUITE_PATH = Path(__file__).with_name("suite.yaml")

VALUE_SOURCES: tuple[ValueSource, ...] = ()

contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)
