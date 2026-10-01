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

Every operation of the spec is in exactly one suite; a unit test fails when
one is in none. `python -m helper.contract suites` lists the suites and how many
operations each one sends, limits or skips.

## Running

The run is a pytest test. It needs a deployment, like the other integration
tests, and it uses their fixtures and their login.

```bash
cd integration-tests
pytest -m contract                                       # every suite
pytest -m contract response-validation/enterprise-search # one suite
pytest -m contract --collect-only -q                     # list the tests; sends nothing
pytest "response-validation/enterprise-search/contract/integration_test_enterprise_search_contract.py::test_spec_matches_api[GET /search]"
```

**Use a deployment that holds no data anyone needs.** The run creates, changes
and deletes users, knowledge bases, conversations, agents and settings. It is
written to change only what its fixtures and its own test cases created, and to
delete both at the end; an operation that would do more is skipped, with the
reason in the suite file.

The suites run one after the other, also under pytest-xdist: they are all in
one xdist group, because two of them must not change the same deployment at once.

Selecting tests also selects what is sent: only the operations of the collected
tests are run. Use node IDs for that; an operation label has characters that
`-k` does not accept.

The tests carry the marker `contract`, not `integration`, so the usual shards
do not run them.

These commands work on files only and send nothing to PipesHub:

```bash
SUITE=response-validation/enterprise-search/contract/suite.yaml
python -m helper.contract plan   $SUITE   # list every generated test case (plan.md)
python -m helper.contract report $SUITE   # write the report of the last run again
python -m helper.contract accept $SUITE   # make baseline.json say what the last run found
python -m helper.contract index           # one page for the last run of every suite
python -m helper.contract suites          # which suite has which operations
```

## Reading the result

One test per operation. `report.md` in the output folder of the suite has the
details; `index.md` one level up adds the suites of a session together.

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

A 3xx answer counts as a success like a 2xx: for a sign-in redirect the 302 is
what the spec documents. The run does not follow a redirect.

"Incomplete" is also the verdict when a request was answered with 429. The rate
limiter answered, not the operation, so the request says nothing about the spec.
Lower the rate in the suite file (`rate_limit`, `operation_rate_limits`).

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
| `values_by_operation` | The same for one operation; it wins over `values` |
| `constants` | Value keys whose value the suite file gives itself, for example `connector.type: Confluence`; a constant can be a number or `true`/`false` |
| `headers` | The value key of a header that every request of an operation carries, for example `Accept` for a stream |
| `client_chosen_ids` | ID fields whose value the client is free to choose, each with a reason |
| `ids_without_fixture` | ID fields that must name something that exists and have no fixture, each with a reason |
| `negative_only` | Operations that are only sent invalid requests, in groups with a reason each (a valid one calls the LLM, sends an email, ...) |
| `examples_only` | Operations whose valid requests are limited to the examples in the spec; all invalid ones are sent |
| `no_success_response` | Operations for which no request can get a 2xx, each with a reason |
| `created_resources` | What a successful test case creates, so the test can delete it |
| `skip` | Operations that are not sent, each with a reason |
| `requires` | Value keys an operation needs that go into no request, for example a fixture that saves a setting and puts it back |
| `auth` | How an operation logs in, where the spec does not decide it (see below) |
| `rate_limit`, `operation_rate_limits` | A lower request rate for the suite or for one operation, for example `30/m` |

A path parameter needs a value key unless the spec lists its values (`enum`):
Schemathesis sends every listed value by itself.

### Login

The spec decides how each operation logs in:

| The spec accepts | The request carries |
|---|---|
| `oauth2` (most operations) | The token of the integration-test OAuth client, as the fixtures do (`oauth_client`) |
| only `bearerAuth` | The session token of the test user, from a password login (`session`) |
| nothing (`security: []`) | No `Authorization` header (`none`) |
| only another scheme (`scopedToken`) | Nothing by default: the suite must give a token or skip the operation |

`auth` overrides this for one operation: `oauth_client`, `session`, `none`, or
`{token: <value key>}` for a token that a fixture provides. `session` needs
`PIPESHUB_TEST_USER_EMAIL` and `PIPESHUB_TEST_USER_PASSWORD`; without them the
operations that need it are "Not run".

The base path comes from the spec too: `/api/v1`, or none for the operations
that the spec puts at the root (`/.well-known/...`, `/mcp`). One suite has one
base path.

### Deciding how an operation is run

The aim is that every operation is run in full. Each step away from that needs
a reason in the suite file, and shows in the report as a coverage gap.

| Run it | When |
|---|---|
| in full (no entry) | A valid request is harmless: it reads, or it creates, changes or deletes only what a fixture of the suite or the request itself created. |
| in full, with `requires` | A valid request changes a setting of the whole organization, and a fixture can save the setting first and put it back at the end. |
| `examples_only` | A valid request costs or reaches outside (see the next row), and the spec has examples that are cheap and safe. |
| `negative_only` | A valid request calls an LLM, sends an email, calls a third party, starts a long job, or needs a real file. Its invalid requests must be rejected before any of that. |
| `skip` | One accepted request, valid or not, can break the deployment or the run, or cannot be undone: it deletes or reconfigures what other suites need, or it ends the login that the run uses. |

Before an operation is limited or skipped, look for a fixture that makes it safe.
`DELETE /users/{id}` is not skipped; it gets a user that a fixture made for it.

`no_success_response` is for an operation that is sent but that no generated
request can satisfy, for example one that needs a one-time code from an email.

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

A value replaces a generated value of its own type (text, number, boolean) that
is already in the request. It is never added. In an invalid request the field
under test keeps its invalid value, and every other field gets its value: the
request must be invalid in one way only, or the API could reject it for a
generated ID and the test would pass without showing that the API saw the
invalid part.

Three special values:

- **A file.** For a file field of a multipart body (`format: binary`), the value
  is the path of a file. The request then carries that file with its name, its
  content and its type. Without one, Schemathesis sends an empty file named after
  the form field, which no API that looks at the extension or the content accepts.
- **`{case}`.** In a text value it becomes a number that is different in each
  request: `contract-team-ab12-{case}`. Use it for a name that must be unique, so
  that every valid create request can succeed, not only the first one.
- **A field under a free-form key** is written `body.roles{*}.modelKey`.

A value for a field that is not an ID has a price. The field is no longer sent
with what Schemathesis generates for it, so the run cannot show that the spec
allows a value that the API rejects (an empty name where the spec has no
`minLength`). It is still the better choice when no valid request can succeed
without it: a success is what shows the status code and the response body.
Write the gap in the spec next to the value in the suite file.
The value must be one that the spec allows for its field. Schemathesis looks at
the request again once the value is in, and if the spec forbids the value (a
pattern, a format, an enum) it counts the request as invalid. When the API then
accepts it, the run reports a difference, and rightly so: the spec does not
describe the IDs that the API uses.
No ID is taken from the response of an earlier request: that Schemathesis
feature is off, so that a request does not depend on what ran before it.

### Roles

Fixtures have module scope: what a suite created and changed is put back when
its tests are done, before the next suite starts.

Each fixture resource has a role, so that operations cannot disturb each other,
in whatever order they run: `readonly` is only read, `mutable` is updated,
`archivable` is archived by the archive operation, `archived` is already
archived for the unarchive operation, `linked` is already in a project for the
project-visibility operation, and `disposable` is deleted by the DELETE
operation under test.

### Fixtures in the report

`VALUE_SOURCES` in the suite's `conftest.py` lists every fixture that gives
values: its value keys, how to read them, one sentence that says what it is, and
whether it was `added` for the contract tests or was an `existing`
integration-test fixture. The report and the plan have a "Fixtures" section made
from that list, with the value each key had in the run, or why it had none.
A value marked `secret` (a token) is not shown, and no file of the run has it.

The API answers with the secrets of the OAuth apps and access tokens that the
test cases create. `redaction.py` masks the text under every field whose name
ends in `secret`, `token`, `password`, `apiKey` and the like, in `events.ndjson`
and `requests.har`, before anything reads them. The run deletes those apps and
tokens at the end.

## How a run works

1. `suite.py` loads the suite and checks it against the spec (`fields.py` finds the ID fields).
2. `pytest_support.py` collects the values from the fixtures that `VALUE_SOURCES` lists
   (`sources.py`).
3. `config.py` decides what is sent and writes the Schemathesis config of the run from
   `schemathesis.base.toml` and the suite.
4. `runner.py` starts Schemathesis once for the suite. `hooks.py`, loaded by Schemathesis, logs
   each operation in and puts the real values into path, query and body (`values.py`).
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
   `baseline.json`, a `conftest.py` and `integration_test_<suite>_contract.py`; copy them
   from `enterprise-search`. The folder must be named `contract`, and the test module after
   the suite: pytest cannot import two test modules with the same file name.
2. Run `python -m helper.contract plan <suite.yaml>`. It tells you which path
   parameters, ID fields and logins the suite has not decided on yet.
3. Write the fixtures in the suite's `conftest.py`, list them in `VALUE_SOURCES`, and end
   the file with `contract_values, contract_run = suite_fixtures(SUITE_PATH, VALUE_SOURCES)`.
   `unit/test_contract_fixtures.py` checks that every key the suite names is provided.

## Tests of this library

`integration-tests/unit/test_contract_*.py` need no deployment. They include a
run of the pytest layer in a throwaway project against a local stub
(`test_contract_pytest_layer.py`), and they check that every suite in the
repository agrees with the spec.
