# API contract tests

These tests check that the PipesHub API behaves as its OpenAPI spec says
(`backend/nodejs/apps/src/modules/api-docs/pipeshub-openapi.yaml`).
[Schemathesis](https://schemathesis.readthedocs.io) generates the requests from
the spec, sends them to a running PipesHub and checks every response.

Design and plan: Confluence "API Reference Health Check" (Jira PA-2593).

This is a local trial. There is one suite, `enterprise-search`: the operations
under `/conversations`, `/search` and `/agents`.

## Setup

```bash
cd integration-tests
uv venv --python 3.12 contract/.venv
uv pip install --python contract/.venv/bin/python -r contract/requirements.txt
```

The tests have their own small environment because `integration-tests/.venv`
carries the full backend dependencies, which they do not need.

The deployment and the login come from `integration-tests/.env` and
`.env.local`, the same files the integration tests use:
`PIPESHUB_BASE_URL`, `PIPESHUB_TEST_USER_EMAIL`, `PIPESHUB_TEST_USER_PASSWORD`.
The tests log in as that user with a session JWT.

## Commands

```bash
PY=contract/.venv/bin/python

$PY contract/run_contract_tests.py plan              # list the generated tests; needs no PipesHub
$PY contract/run_contract_tests.py run --yes         # create fixtures, run, write the report card
$PY contract/run_contract_tests.py report            # write the report card again from the last run
$PY contract/run_contract_tests.py cleanup --yes     # delete the fixtures and what the tests created
```

`run` options: `--read-only` (GET only), `--only OPERATION_ID` (repeatable),
`--fresh-fixtures`, `--fuzz`, `--llm-examples`.

Everything is written to `integration-tests/reports/contract/<suite>/`, which
git ignores: `plan.md`, `report.md`, `report.json`, `events.ndjson` (every test
case with its request and response), `requests.har`, `schemathesis.toml` (the
exact config of the run) and `fixtures.json`.

## The deployment must be disposable

`fixtures`, `run` and `cleanup` create, change and delete data on the deployment
in `PIPESHUB_BASE_URL`. They stop unless you confirm, with `--yes` or at the
prompt. Use a deployment that holds no data anyone needs.

A run changes only what it created: its fixtures and the resources its own test
cases create (for example agents from `createAgent`). `cleanup` deletes both.

The deployment needs an LLM configured, to create the conversation and agent
fixtures, and at least one indexed document, to create the search fixtures.
Operations whose fixture is missing are reported as "Not tested".

## How a run works

1. `suites/<suite>.yaml` selects the operations and says how each one is run.
2. `fixtures.py` creates conversations, agents and searches, so that path
   parameters such as `{conversationId}` are real IDs. Each fixture has a role
   (`readonly`, `mutable`, `archivable`, `disposable`), so a DELETE test cannot
   break a GET test.
3. `config_builder.py` merges `schemathesis.base.toml`, the suite and the
   fixture IDs into the config of the run.
4. Schemathesis runs the `examples` and `coverage` phases. Both are
   deterministic, so two runs send the same cases. `coverage` includes the
   boundary values of every constraint: for `minimum: 1, maximum: 100` it sends
   0, 1, 100 and 101.
5. `report.py` reads the results and grades every operation.

### Checks

| Check | Fails when |
|---|---|
| `status_code_conformance` | the status code is not in the spec |
| `content_type_conformance` | the Content-Type is not in the spec |
| `response_schema_conformance` | the body does not match the response schema |
| `positive_data_acceptance` | the API rejects a request that the spec allows |
| `negative_data_rejection` | the API accepts a request that the spec forbids |
| `not_a_server_error` | the API returns 5xx |

### Grades

| Grade | Meaning |
|---|---|
| Verified | In the SDK (`x-pipeshub-sdk: true`), tested, no failure |
| Matches | Tested, no failure |
| Drift | The API and the spec do not agree |
| Server error | The API returned 5xx, and nothing else failed |
| Partly tested | No failure, but no 2xx response was checked |
| Not tested | No request was sent; the report gives the reason |

### How an operation is run

| In the suite file | What is sent |
|---|---|
| (default) | Positive and negative cases |
| `negative_only` | Negative cases only. Used where a valid request calls the LLM. |
| `skip` | Nothing. The reason is in the report. |

`negative_only` relies on the backend rejecting an invalid request before it
reaches the LLM. If the backend accepts one, the LLM is called, and the report
shows it as a `negative_data_rejection` finding.

## Adding a suite

Add `suites/<name>.yaml` with `include_path_regex`, the fixture key for every
path parameter, and the `negative_only` and `skip` lists. Add the fixtures it
needs to `fixtures.py`. Then run `plan --suite <name>`; it fails with a clear
message if the suite and the spec do not agree.
