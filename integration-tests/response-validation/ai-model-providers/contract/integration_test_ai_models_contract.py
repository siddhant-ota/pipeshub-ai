"""Contract tests for the AI models: the OpenAPI spec must describe what the API does.

Schemathesis generates requests from ``pipeshub-openapi.yaml`` for every
operation under ``/configurationManager/ai-models`` and
``/configurationManager/aiModelsConfig`` and sends them once (``contract_run``).
Each test below reads the result for one operation.

The API is the reference. A test fails when the spec and the API differ in a
way that ``baseline.json`` does not list, when the baseline lists a difference
that no longer occurs, or when the operation could not be checked. It does not
fail because the API behaves badly; it fails because the spec says otherwise.

How each operation is run is in ``suite.yaml``; the library is
``helper/contract`` (see its README).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helper.contract.outcome import assert_spec_matches_api
from helper.contract.pytest_support import CONTRACT_MARKS, operation_params
from helper.contract.runner import ContractRun
from helper.contract.suite import load_suite

SUITE = load_suite(Path(__file__).with_name("suite.yaml"))

pytestmark = CONTRACT_MARKS


@pytest.mark.parametrize("operation_id", operation_params(SUITE))
def test_spec_matches_api(operation_id: str, contract_run: ContractRun) -> None:
    assert_spec_matches_api(contract_run.result_for(operation_id), contract_run.files.report)
