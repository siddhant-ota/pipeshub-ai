"""The pytest side of a contract suite. Every suite uses it the same way.

In the suite's `conftest.py`, after its fixtures (module scope, so that they are torn down
when the tests of the suite are done, before the next suite starts) and `VALUE_SOURCES`:

    contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)

In its test module, `integration_test_<suite>_contract.py`:

    SUITE = load_suite(SUITE_PATH)
    pytestmark = CONTRACT_MARKS

    @pytest.mark.parametrize("operation_id", operation_params(SUITE))
    def test_spec_matches_api(operation_id: str, contract_run: ContractRun) -> None:
        assert_spec_matches_api(contract_run.result_for(operation_id), contract_run.files.report)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import requests

from helper.contract.baseline import BaselineError
from helper.contract.created import created_resources
from helper.contract.report import render_index, summary_lines
from helper.contract.runner import (
    BASELINE_NAME,
    INDEX_PATH,
    SUITE_NAME,
    ContractRun,
    RunnerError,
    execute,
    judge,
    run_files,
)
from helper.contract.sources import ValueSource, fixture_rows
from helper.contract.suite import (
    AUTH_OAUTH_CLIENT,
    AUTH_SESSION,
    AUTH_TOKEN,
    PROFILE_SKIP,
    Suite,
    SuiteError,
    load_suite,
)
from helper.contract.values import ContractValues

logger = logging.getLogger("contract")

# pytest imports a test module by its base name, so each suite names its module after itself:
# integration_test_<suite>_contract.py.
TEST_MODULE_GLOB = "integration_test_*_contract.py"
# The fixture that gives each login its client, for deleting what the test cases created.
_CLIENT_FIXTURES = {AUTH_OAUTH_CLIENT: "pipeshub_client", AUTH_SESSION: "user_session_client"}

# How long a delete of what a test case created may take.
_DELETE_TIMEOUT_SEC = 60

CONTRACT_MARKS = [
    pytest.mark.contract,
    # One group for all suites: a suite's run is one fixture, so its tests must be on
    # the worker that has it, and two suites must not change the same deployment at once.
    # It is the group the root conftest pins the other serial suites to.
    pytest.mark.xdist_group("serial"),
]

# Whatever makes a fixture fail, the effect here is the same: its values are missing.
_FIXTURE_FAILURES = (Exception, pytest.fail.Exception, pytest.skip.Exception)


def response_body(resp: requests.Response, expected: tuple[int, ...], what: str) -> dict[str, Any]:
    """The JSON object of a response that a fixture needs, or an assertion that says what failed."""
    assert resp.status_code in expected, f"{what}: HTTP {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, dict), f"{what}: expected a JSON object, got {body!r}"
    return body


def _quietly(
    verb: str, what: str, call: Callable[[], requests.Response], also_fine: tuple[int, ...] = ()
) -> bool:
    """Make one teardown call. A failure is logged and must not stop the calls after it."""
    try:
        resp = call()
    except Exception as exc:  # noqa: BLE001 - for example a token that cannot be renewed
        logger.warning("Could not %s %s: %s", verb, what, exc)
        return False
    done = resp.status_code < 400 or resp.status_code in also_fine
    if not done:
        logger.warning("Could not %s %s: HTTP %s %s", verb, what, resp.status_code, resp.text[:200])
    return done


def delete_quietly(what: str, delete: Callable[[], requests.Response]) -> None:
    """Delete one thing in a fixture teardown."""
    # 404: the operation under test already removed it.
    _quietly("delete", what, delete, also_fine=(404,))


def restore_quietly(what: str, restore: Callable[[], requests.Response]) -> bool:
    """Put one setting back in a fixture teardown. False if the API refused it."""
    return _quietly("restore", what, restore)


def operation_params(suite: Suite) -> list[Any]:
    """One test parameter for each operation of the suite."""
    return [
        pytest.param(
            planned.operation.operation_id,
            id=planned.operation.label,
            # Skipped here, at collection, so that a skipped operation never starts the run.
            marks=pytest.mark.skip(reason=planned.reason)
            if planned.profile == PROFILE_SKIP
            else (),
        )
        for planned in suite.operations
    ]


def _failure(fixture: str, exc: BaseException) -> str:
    return f"fixture `{fixture}` failed: {type(exc).__name__}: {str(exc)[:300]}"


def collect_values(
    request: pytest.FixtureRequest, suite: Suite, sources: tuple[ValueSource, ...]
) -> ContractValues:
    """Every value the suite names. A fixture that fails takes only its own values with it.

    The operations that need a missing value are not sent and their tests fail
    with the reason, while the rest of the suite still runs.
    """
    collected = ContractValues(values=dict(suite.constants))
    for source in sources:
        try:
            values = source.read(request.getfixturevalue(source.fixture))
        except _FIXTURE_FAILURES as exc:  # noqa: BLE001
            reason = _failure(source.fixture, exc)
            logger.warning("Contract values: %s", reason)
            collected.missing.update(dict.fromkeys(source.keys, reason))
        else:
            collected.values.update(zip(source.keys, values, strict=True))
            if source.secret:
                collected.secret.update(source.keys)
    if AUTH_SESSION in suite.logins:
        fixture = _CLIENT_FIXTURES[AUTH_SESSION]
        try:
            request.getfixturevalue(fixture)
        except _FIXTURE_FAILURES as exc:  # noqa: BLE001
            collected.no_login[AUTH_SESSION] = _failure(fixture, exc)
    return collected


def _selected_operations(session: pytest.Session, suite_directory: Path) -> set[str]:
    """The operations whose tests this session runs, so that selecting tests limits what is sent."""
    callspecs = (
        getattr(item, "callspec", None)
        for item in session.items
        if item.path.parent == suite_directory
    )
    return {callspec.params["operation_id"] for callspec in callspecs if callspec is not None}


def _delete_leftovers(request: pytest.FixtureRequest, suite: Suite, values: ContractValues) -> None:
    """Delete what the test cases themselves created, for example agents from `createAgent`.

    `execute` removes the events of the run before, so these are from this run only.
    """

    def _delete(fixture: str, path: str) -> requests.Response:
        return request.getfixturevalue(fixture).request("DELETE", f"{suite.api_prefix}{path}")

    def _delete_with_token(token: str, path: str) -> requests.Response:
        base_url = request.getfixturevalue(_CLIENT_FIXTURES[AUTH_OAUTH_CLIENT]).base_url
        return requests.delete(
            f"{base_url}{suite.api_prefix}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_DELETE_TIMEOUT_SEC,
        )

    for leftover in created_resources(suite, values, run_files(suite).events):
        if "{" in leftover.path:
            logger.warning("Not deleted, a value in its path is missing: %s", leftover.path)
        elif leftover.auth == AUTH_TOKEN:
            token = str(values.values[leftover.token_key])
            delete_quietly(leftover.path, partial(_delete_with_token, token, leftover.path))
        elif leftover.auth in _CLIENT_FIXTURES:
            fixture = _CLIENT_FIXTURES[leftover.auth]
            delete_quietly(leftover.path, partial(_delete, fixture, leftover.path))
        else:
            logger.warning("Not deleted, no login `%s`: %s", leftover.auth, leftover.path)


def suite_fixtures(suite_path: Path, sources: tuple[ValueSource, ...]) -> tuple[Any, Any]:
    """The `contract_values` and `contract_run` fixtures of the suite at `suite_path`."""

    # Module scope: what the suite created and changed is put back when its tests are done,
    # and not at the end of the session, when the other suites have already run with it.
    @pytest.fixture(scope="module", name="contract_values")
    def contract_values(request: pytest.FixtureRequest) -> ContractValues:
        return collect_values(request, load_suite(suite_path), sources)

    @pytest.fixture(scope="module", name="contract_run")
    def contract_run(
        request: pytest.FixtureRequest, pipeshub_client: Any, contract_values: ContractValues
    ) -> Iterator[ContractRun]:
        """Sends the suite's test cases once and judges the answers."""
        suite = load_suite(suite_path)
        try:
            yield execute(
                suite,
                contract_values,
                base_url=pipeshub_client.base_url,
                baseline_path=suite_path.with_name(BASELINE_NAME),
                selected=_selected_operations(request.session, suite_path.parent),
                fixtures=fixture_rows(suite, sources),
            )
        finally:
            _delete_leftovers(request, suite, contract_values)

    return contract_values, contract_run


def _suites_that_ran(terminalreporter: Any) -> list[Path]:
    """The suite files whose tests reached their call phase in this session."""
    root = terminalreporter.config.rootpath
    directories = {
        (root / report.fspath).parent
        for reports in terminalreporter.stats.values()
        for report in reports
        if getattr(report, "when", "") == "call" and getattr(report, "fspath", "")
    }
    return sorted(
        directory / SUITE_NAME for directory in directories if (directory / SUITE_NAME).exists()
    )


def terminal_summary(terminalreporter: Any) -> None:
    """Say what each contract run covered: a run with no failure can still have gaps by design.

    Called from `pytest_terminal_summary` of the root conftest, so that it also
    runs in the controller of a pytest-xdist session.
    """
    runs: list[ContractRun] = []
    for suite_path in _suites_that_ran(terminalreporter):
        try:
            runs.append(judge(load_suite(suite_path), suite_path.with_name(BASELINE_NAME)))
        except (SuiteError, RunnerError, BaselineError):
            continue
    for run in runs:
        terminalreporter.section(f"API contract: {run.suite.name}")
        for line in summary_lines(list(run.results)):
            terminalreporter.write_line(line)
        terminalreporter.write_line(f"Report: {run.files.report}")
    if len(runs) > 1:
        INDEX_PATH.write_text(
            render_index([(run.meta, list(run.results), run.files.report) for run in runs]),
            encoding="utf-8",
        )
        terminalreporter.section("API contract: all suites")
        terminalreporter.write_line(f"Report: {INDEX_PATH}")
