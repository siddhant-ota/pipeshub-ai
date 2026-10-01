"""Real values go into a request without changing what the test case is about."""

from __future__ import annotations

import pytest

from helper.contract.values import (
    Mutation,
    Substitution,
    is_under_test,
    parse_field,
    substitute,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("query.projectId", ("query", ("projectId",))),
        ("body.filters.kb[*]", ("body", ("filters", "kb", "*"))),
        ("body.models[*].modelKey", ("body", ("models", "*", "modelKey"))),
        ("body.matrix[*][*]", ("body", ("matrix", "*", "*"))),
    ],
)
def test_parse_field(name: str, expected: tuple) -> None:
    assert parse_field(name) == expected


@pytest.mark.parametrize("name", ["path.id", "body", "query."])
def test_parse_field_rejects_what_is_not_a_body_or_query_field(name: str) -> None:
    with pytest.raises(ValueError, match="starts with"):
        parse_field(name)


def test_substitute_replaces_every_string_at_the_path() -> None:
    body = {"filters": {"kb": ["a", "b"], "apps": ["c"]}, "query": "q"}

    replaced, count = substitute(body, Substitution.for_field("body.filters.kb[*]", "KB"))

    assert replaced == {"filters": {"kb": ["KB", "KB"], "apps": ["c"]}, "query": "q"}
    assert count == 2
    assert body["filters"]["kb"] == ["a", "b"], "the original request must not change"


def test_substitute_reaches_into_objects_in_an_array() -> None:
    body = {"models": [{"modelKey": "x", "provider": "p"}, {"provider": "p"}]}

    replaced, count = substitute(body, Substitution.for_field("body.models[*].modelKey", "KEY"))

    assert replaced == {"models": [{"modelKey": "KEY", "provider": "p"}, {"provider": "p"}]}
    assert count == 1


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q"},
        {"filters": {}},
        {"filters": {"kb": []}},
        # Not a string: a negative case put a wrong type here on purpose.
        {"filters": {"kb": [None, 3]}},
        {"filters": "not an object"},
        "not an object",
        None,
    ],
)
def test_substitute_never_adds_a_field_or_fixes_a_wrong_type(body: object) -> None:
    replaced, count = substitute(body, Substitution.for_field("body.filters.kb[*]", "KB"))

    assert replaced == body
    assert count == 0


def test_substitute_a_query_parameter() -> None:
    replaced, count = substitute(
        {"projectId": "random", "limit": "5"}, Substitution.for_field("query.projectId", "P")
    )

    assert replaced == {"projectId": "P", "limit": "5"}
    assert count == 1


KB = Substitution.for_field("body.filters.kb[*]", "KB")
PROJECT = Substitution.for_field("query.projectId", "P")


@pytest.mark.parametrize(
    ("substitution", "mutation", "expected"),
    [
        # A valid part of the request.
        (KB, None, False),
        # The case is about this field, its parent, or the whole body.
        (KB, Mutation("body", "application/json", "/properties/filters/properties/kb/items/type"), True),
        (KB, Mutation("body", "application/json", "/properties/filters/type"), True),
        (KB, Mutation("body", "application/json", "/type"), True),
        (KB, Mutation("body", "application/json", "/allOf/0/properties/filters/properties/kb/maxItems"), True),
        # The case is about another body field: the ID must still be real.
        (KB, Mutation("body", "application/json", "/properties/filters/properties/apps/items/type"), False),
        (KB, Mutation("body", "application/json", "/properties/query/minLength"), False),
        # The case is about another part of the request.
        (KB, Mutation("query", "limit", ""), False),
        (PROJECT, Mutation("query", "projectId", ""), True),
        (PROJECT, Mutation("query", "limit", ""), False),
        (PROJECT, Mutation("body", "application/json", "/type"), False),
    ],
)
def test_the_field_under_test_keeps_its_invalid_value(
    substitution: Substitution, mutation: Mutation | None, expected: bool
) -> None:
    assert is_under_test(substitution, mutation) is expected
