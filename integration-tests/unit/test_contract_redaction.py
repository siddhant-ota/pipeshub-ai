"""The files of a run must not hold credentials: they are kept as build artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from contract_samples import SPEC, case_event, write_events, write_suite

from helper.contract.created import created_resources
from helper.contract.events import read_cases
from helper.contract.redaction import MASK, mask_body, mask_text, redact_run_files
from helper.contract.suite import load_suite
from helper.contract.values import ContractValues

pytestmark = pytest.mark.unit

CREATED_APP = {
    "app": {
        "id": "app-1",
        "clientId": "client-1",
        "clientSecret": "s3cr3t",
        "tokens": [{"id": "t1", "access_token": "access-1", "refreshToken": "rt-1"}],
    },
    # An object under a credential name is not a credential; its ID is needed for the cleanup.
    "token": {"id": "pat-1", "token": "phpat_raw"},
    "nextPageToken": "",
}


@pytest.mark.parametrize(
    ("body", "masked"),
    [
        ('{"password": "hunter2", "email": "a@b.c"}', {"password": MASK, "email": "a@b.c"}),
        (
            '{"apiKey": "k", "api_key": "k", "privateKey": "k"}',
            dict.fromkeys(("apiKey", "api_key", "privateKey"), MASK),
        ),
        ('[{"clientSecret": "x"}]', [{"clientSecret": MASK}]),
        # Not credentials: an ID of a token, a number, an empty text.
        ('{"tokenId": "t1", "maxTokens": 5, "token": ""}', None),
        ('{"name": "contract"}', None),
    ],
)
def test_mask_body_masks_the_credential_fields_of_json(body: str, masked: object) -> None:
    assert json.loads(mask_body(body)) == (json.loads(body) if masked is None else masked)


def test_mask_body_masks_a_form_body_and_leaves_other_text() -> None:
    form = "grant_type=client_credentials&client_id=c1&client_secret=s3cr3t"

    assert (
        mask_body(form) == "grant_type=client_credentials&client_id=c1&client_secret=%5Bmasked%5D"
    )
    assert mask_body("event: token\ndata: hello") == "event: token\ndata: hello"
    assert mask_body("") == ""


def test_the_files_of_a_run_lose_their_credentials_and_keep_their_ids(tmp_path: Path) -> None:
    events = write_events(
        tmp_path,
        [
            case_event(
                "POST /things",
                case_id="a",
                status=201,
                query="token=fixture-token",
                body={"client_secret": "sent-secret", "name": "n"},
                response=CREATED_APP,
            )
        ],
    )
    har = tmp_path / "requests.har"
    har.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "url": "http://x/api/v1/things?token=fixture-token",
                                "postData": {"text": '{"client_secret": "sent-secret"}'},
                            },
                            "response": {"content": {"text": json.dumps(CREATED_APP)}},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    redact_run_files(events=[events], hars=[har], logs=[], secrets={"fixture-token"})

    for path in (events, har):
        text = path.read_text(encoding="utf-8")
        assert "fixture-token" not in text, "a value that a fixture marked as a credential"
    (case,) = read_cases(events)
    assert case.request_json() == {"client_secret": MASK, "name": "n"}
    response = case.response_json()
    assert response["app"]["clientSecret"] == MASK
    assert response["app"]["tokens"] == [{"id": "t1", "access_token": MASK, "refreshToken": MASK}]
    assert response["token"] == {"id": "pat-1", "token": MASK}
    assert response["app"]["clientId"] == "client-1"
    for secret in ("s3cr3t", "sent-secret", "access-1", "rt-1", "phpat_raw"):
        assert secret not in har.read_text(encoding="utf-8")


def test_the_cleanup_still_finds_what_was_created(tmp_path: Path) -> None:
    suite = load_suite(
        write_suite(
            tmp_path,
            created_resources=[
                {
                    "operation": "createThing",
                    "id_pointer": "/token/id",
                    "delete_path": "/things/{id}",
                }
            ],
        ),
        SPEC,
    )
    events = write_events(
        tmp_path, [case_event("POST /things", case_id="a", status=201, response=CREATED_APP)]
    )

    redact_run_files(events=[events], hars=[tmp_path / "no.har"], logs=[], secrets=set())

    assert [leftover.path for leftover in created_resources(suite, ContractValues(), events)] == [
        "/things/pat-1"
    ]


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VySWQiOiJ1MSJ9.c2lnbmF0dXJl"


def test_the_login_of_the_run_is_not_in_the_report(tmp_path: Path) -> None:
    """Schemathesis writes the headers of each case into its NDJSON report as they are."""
    event = case_event("GET /things", case_id="a")
    value = event["ScenarioFinished"]["recorder"]["cases"]["a"]["value"]
    value["headers"] = {"Authorization": f"Bearer {JWT}", "Accept": "*/*"}
    value["query"] = {"access_token": "raw-query-token", "limit": 5}
    value["body"] = {"password": "hunter2", "name": "n"}
    events = write_events(tmp_path, [event])

    redact_run_files(events=[events], hars=[], logs=[], secrets=set())

    text = events.read_text(encoding="utf-8")
    assert JWT not in text and "raw-query-token" not in text and "hunter2" not in text
    masked = json.loads(text.splitlines()[1])["ScenarioFinished"]["recorder"]["cases"]["a"]["value"]
    assert masked["headers"] == {"Authorization": MASK, "Accept": "*/*"}
    assert masked["query"] == {"access_token": MASK, "limit": 5}
    assert masked["body"] == {"password": MASK, "name": "n"}


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        # What a failed check quotes from a response, also cut off at the end of a line.
        ('Value:\n    "clientSecret": "0123456789abcdef0123456789abcdef",', "0123456789abcdef"),
        ('    "clientSecret": "0123456789abcdef0123', "0123456789abcdef0123"),
        # The same inside a JSON string, as the NDJSON report holds a check message.
        ('{"message": "\\"refresh_token\\": \\"rt-secret-value\\""}', "rt-secret-value"),
        # A curl command in the log.
        ("curl -X POST -d 'grant_type=x&client_secret=s3cr3t-form' http://h/token", "s3cr3t-form"),
        (f"curl -H 'Authorization: Bearer {JWT}' http://h/api", JWT),
        ("the token phpat_AbCdEf0123456789 was created", "phpat_AbCdEf0123456789"),
        ("service token phsvc_AbCdEf0123456789", "phsvc_AbCdEf0123456789"),
    ],
)
def test_mask_text_finds_credentials_without_parsing(text: str, secret: str) -> None:
    masked = mask_text(text)

    assert secret not in masked
    assert MASK in masked


def test_mask_text_leaves_what_is_no_credential() -> None:
    text = '"tokenId": "t-1", "name": "contract", "maxTokens": 5, "limit=20&page=2"'

    assert mask_text(text) == text


def test_the_log_is_masked_and_a_broken_report_keeps_no_secret(tmp_path: Path) -> None:
    log = tmp_path / "schemathesis.log"
    log.write_text(
        f'Response: {{"clientSecret": "from-the-log"}}\nBearer {JWT}\n', encoding="utf-8"
    )
    events = write_events(tmp_path, [case_event("GET /things", case_id="a")])
    # A run that was killed leaves a last line that is cut off.
    with events.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"ScenarioFinished": {"recorder": {"cases": {"b": {"value": {"headers": {"Aut'
        )
    har = tmp_path / "requests.har"
    har.write_text('{"log": {"entries": [{"request": {"postData": {"text": "cut', encoding="utf-8")

    redact_run_files(events=[events], hars=[har], logs=[log], secrets=set())

    assert "from-the-log" not in log.read_text(encoding="utf-8")
    assert JWT not in log.read_text(encoding="utf-8")
    assert len(list(read_cases(events))) == 1, "the line that could be read is kept"
    assert not har.exists(), "a HAR file that cannot be read cannot be kept free of secrets"
