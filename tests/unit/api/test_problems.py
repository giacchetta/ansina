from __future__ import annotations

from typing import ClassVar

import pytest

from ansina.api.problems import problem_response, status_for_error
from ansina.auth.management import LastAdminError, NotFoundError, SelfEscalationError
from ansina.auth.repositories import DuplicateError, UnknownSubjectError
from ansina.errors import AnsinaError, ConfigurationError


class _UnmappedError(AnsinaError):
    """No entry in `_STATUS_BY_ERROR_TYPE` of its own — exercises the `AnsinaError`
    fallback in `status_for_error`.
    """

    code: ClassVar[str] = "ansina.test.unmapped"


def test_status_for_error_uses_own_mapping() -> None:
    assert status_for_error(ConfigurationError("bad config")) == 500


def test_status_for_error_falls_back_through_mro() -> None:
    # `_UnmappedError` isn't in the mapping table itself, so this walks up its MRO to
    # `AnsinaError`'s entry rather than the module-level default.
    assert status_for_error(_UnmappedError("oops")) == 500


@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        (SelfEscalationError, 403),
        (LastAdminError, 409),
        (DuplicateError, 409),
        (NotFoundError, 404),
        (UnknownSubjectError, 404),
    ],
)
def test_status_for_error_issue_27_mappings(
    error_type: type[AnsinaError], expected_status: int
) -> None:
    assert status_for_error(error_type("boom")) == expected_status


def test_problem_response_reads_extra_members() -> None:
    response = problem_response(
        status=418,
        code="ansina.test.teapot",
        title="I'm a teapot",
        detail="short and stout",
        extra={"field": "spout"},
    )

    assert response.status_code == 418
    assert response.media_type == "application/problem+json"
