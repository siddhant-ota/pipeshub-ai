# API contract tests

These tests answer one question: **does the OpenAPI spec describe what the API
does?** The spec is `backend/nodejs/apps/src/modules/api-docs/pipeshub-openapi.yaml`.

The API is the reference. A test does not fail because the API behaves badly;
it fails because the spec says something else. If the API accepts `page=5000`
and the spec says `maximum: 1000`, that is a difference, and the usual fix is
to make the spec say what the API does.

[Schemathesis](https://schemathesis.readthedocs.io) generates the requests from
the spec. It sends valid requests (the spec allows them, so the API must accept
them) and invalid ones (the spec forbids them, so the API must reject them),
including the boundary values of every constraint: for `minimum: 1, maximum: 100`
it sends 0, 1, 100 and 101. It then checks every response against the spec.

## Layout

| What | Where |
|---|---|
| This library | `integration-tests/helper/contract/` |
| A suite: its operations, fixtures and tests | `integration-tests/response-validation/<module>/contract/` |
| Output of a run | `integration-tests/reports/contract/<suite>/` (git ignores it) |

There is one suite so far, `enterprise-search`: the operations under
`/conversations`, `/search` and `/agents`.

## Running

The run is a pytest test. It needs a deployment, like the other integration
tests, and it uses their fixtures and their login.

```bash
cd integration-tests
pytest -m contract                                  # the whole suite, about 4,300 requests
pytest -m contract --collect-only -q                # list the tests; sends nothing
pytest "response-validation/enterprise-search/contract/integration_test_contract.py::test_spec_matches_api[GET /search]"
```

**Use a deployment that holds no data anyone needs.** The run creates, changes
and deletes conversations, agents and searches. It changes only what its
fixtures and its own test cases created, and deletes both at the end.

Selecting tests also selects what is sent: only the operations of the collected
tests are run. Use node IDs for that; an operation label has characters that
`-k` does not accept.

The tests carry the marker `contract`, not `integration`, so the usual shards
do not run them.

Three commands work on files only and send nothing to PipesHub:

```bash
SUITE=response-validation/enterprise-search/contract/suite.yaml
python -m helper.contract plan   $SUITE   # list every generated test case (plan.md)
python -m helper.contract report $SUITE   # write the report of the last run again
python -m helper.contract accept $SUITE   # make baseline.json say what the last run found
```

## Reading the result

One test per operation. `report.md` in the output folder has the details.

| pytest | Verdict | Meaning |
|---|---|---|
| passed | Matches | The spec and the API agree, and a success response was checked |
| passed | Partly checked | No difference; by design the success response is not checked (see the suite file) |
| xfailed | Known difference | Differences found; `baseline.json` lists every one |
| failed | Differs | A difference that the baseline does not list |
| failed | Stale baseline | The baseline lists a difference that no longer occurs; remove the entry |
| failed | Stale suite entry | The suite file says no request can succeed, and one did; remove the entry |
| failed | Unverified | No difference, but the responses that would show one never came (see below) |
| failed | Incomplete | A request got no response, or Schemathesis could not finish the operation |
| failed | Not run | A value the operation needs is missing, for example a fixture failed |
| skipped | Skipped | The suite file skips the operation, with a reason |

"Unverified" means the operation was not really exercised: no request got a
2xx, so the success response was never checked; or, for an operation that is
only sent invalid requests, none was answered with 400 or 422, so nothing shows
that they reached its validation (a stale ID answers 404 to everything).

If Schemathesis stops before the end, for example because the API stops
answering, the whole run is an error and nothing is judged.

A run with no failure can still leave things unchecked. The terminal summary
and the "Coverage gaps" section of the report list every such operation and why.

A difference is one of:

| Check | The difference |
|---|---|
| `negative_data_rejection` | The spec forbids a request; the API accepted it |
| `positive_data_acceptance` | The spec allows a request; the API rejected it |
| `status_code_conformance` | The API answered with a status code the spec does not list |
| `content_type_conformance` | The API answered with a Content-Type the spec does not list |
| `response_schema_conformance` | The response body does not match the schema in the spec |

A 5xx response is not a difference by itself. It is one when the spec does not
document it, and then `status_code_conformance` reports it.

### The baseline

`baseline.json`, next to the suite file, lists the differences that are known
and accepted for now. A test fails only for a difference that is not in it, and
for an entry that no longer occurs, so the file can only get shorter. A
difference is identified by operation, check, field and rule, for example
`searchHistory | negative_data_rejection | query.page | Value greater than maximum`
or `listAgents | response_schema_conformance | status 200 /components/schemas/Toolset/properties/instanceId | type "string"`.
No value from a response is part of it, so it is the same in every run.

After a run, `python -m helper.contract accept <suite.yaml>` rewrites the
baseline from what the run found. It keeps anything you added to an entry (a
ticket, a note) and the entries of operations that the run did not send or did
not finish.
Review the diff of `baseline.json` like any other change: every added line is a
place where the spec is wrong.

## The suite file

`suite.yaml` says how each operation is run. Loading it checks it against the
spec and fails, before any request is sent, when they do not agree.

| Key | What it says |
|---|---|
| `include_path_regex` | Which paths of the spec are in scope |
| `path_parameters` | The value key for each path parameter, for example `conversationId: conversation.mutable.id` |
| `values` | Query and body fields that get a real value, for example `body.filters.kb[*]: knowledgeBase.id` |
| `client_chosen_ids` | ID fields whose value the client is free to choose, each with a reason |
| `ids_without_fixture` | ID fields that must name something that exists and have no fixture, each with a reason |
| `negative_only` | Operations that are only sent invalid requests, because a valid one calls the LLM |
| `examples_only` | Operations whose valid requests are limited to the examples in the spec; all invalid ones are sent |
| `no_success_response` | Operations for which no request can get a 2xx, each with a reason |
| `created_resources` | What a successful test case creates, so the test can delete it |
| `skip` | Operations that are not sent, each with a reason |

### Why IDs need real values

Schemathesis fills an ID field with a random string. The API then answers "not
found", and no valid request with that field ever succeeds. Nothing fails, so
the gap would be invisible. Therefore:

- Every field that holds an ID must be in one of three lists. A field counts as
  an ID when it is named `id`, `ids` or `key`, when its name ends in `Id`, `Ids`
  or `Key`, when it has `format: uuid`, or when its description speaks of ids.
  A new such field in the spec makes the suite fail to load until someone
  decides on it.
  - `values`: it gets a real value from a fixture.
  - `client_chosen_ids`: any value is valid, for example a run ID that the
    client makes up. It keeps the generated value.
  - `ids_without_fixture`: it must name something that exists, and no fixture
    provides that. It keeps the generated value, so a valid request with it
    names nothing real. If the API rejects such a request, that is not counted
    as a difference; the report says how many there were.
- The value keys (`knowledgeBase.id`, ...) come from pytest fixtures, listed in
  `VALUE_SOURCES` in the suite's `conftest.py`.
- If a fixture fails, its values are missing. The operations that need them are
  not sent and their tests fail with the reason; the rest still run.

A value replaces a string that is already in the generated request. It is never
added, and in an invalid request the field under test keeps its invalid value.
No ID is taken from the response of an earlier request: that Schemathesis
feature is off, so that a request does not depend on what ran before it.

### Roles

Each fixture resource has a role, so that operations cannot disturb each other,
in whatever order they run: `readonly` is only read, `mutable` is updated,
`archivable` is archived by the archive operation, `archived` is already
archived for the unarchive operation, `linked` is already in a project for the
project-visibility operation, and `disposable` is deleted by the DELETE
operation under test.

## How a run works

1. `suite.py` loads the suite and checks it against the spec (`fields.py` finds the ID fields).
2. The suite's `conftest.py` collects the values from the fixtures.
3. `config.py` decides what is sent and writes the Schemathesis config of the run from
   `schemathesis.base.toml`, the suite and the values.
4. `runner.py` starts Schemathesis once. `hooks.py`, loaded by Schemathesis, logs in with the
   integration-test client and puts the real values into query and body fields (`values.py`).
5. `events.py` reads the NDJSON report; `results.py` turns failed checks into differences,
   compares them with the baseline (`baseline.py`) and gives each operation a verdict.
6. `report.py` writes `report.md` and `report.json`; `outcome.py` turns a verdict into a
   pytest outcome; `created.py` finds what the test cases created, for cleanup.

Only the `examples` and `coverage` phases of Schemathesis run. Both are
deterministic, so two runs send the same cases and a difference keeps its
identity. Fuzzing and stateful testing are off.

Files of a run, in `reports/contract/<suite>/run/`: `report.md`, `report.json`,
`events.ndjson` (every case with its request, response and checks),
`requests.har`, `schemathesis.toml` (the exact config), `schemathesis.log`.
`CONTRACT_REPORTS_DIR` moves the output folder.

## Adding a suite

1. Create `response-validation/<module>/contract/` with a `suite.yaml`, an empty
   `baseline.json`, a `conftest.py` and a test module; copy them from `enterprise-search`.
2. Run `python -m helper.contract plan <suite.yaml>`. It tells you which path
   parameters and ID fields the suite has not decided on yet.
3. Provide the values from that module's fixtures in `VALUE_SOURCES`.
   `unit/test_contract_fixtures.py` checks that every key the suite names is provided.

## Tests of this library

`integration-tests/unit/test_contract_*.py` need no deployment. They include a
run of the pytest layer in a throwaway project against a local stub
(`test_contract_pytest_layer.py`), and they check that every suite in the
repository agrees with the spec.
