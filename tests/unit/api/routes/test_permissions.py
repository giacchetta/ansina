from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


def test_read_role_gets_403(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("read")

    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ansina.forbidden"


def test_maintain_without_sudo_can_still_read(
    authed_client: TestClient, token_for_role: Callable[[str], str]
) -> None:
    token = token_for_role("maintain")

    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_lists_each_resource_with_its_served_verbs_policy_class_and_grantability(
    authed_client: TestClient, authed_token: str
) -> None:
    response = authed_client.get(
        "/auth/permissions", headers={"Authorization": f"Bearer {authed_token}"}
    )

    assert response.status_code == 200
    body = response.json()
    by_resource = {entry["resource"]: entry for entry in body}

    # Issue #38 AC: a GET-only route lists only the verbs it actually serves, not
    # every `Verb` — `system.version` only ever answers GET.
    assert by_resource["system.version"]["verbs"] == ["GET"]
    assert by_resource["system.version"]["policy_class"] == "ordinary"
    assert by_resource["system.version"]["grantable"] is True

    # `auth.users` is served by every verb across its several routes.
    assert set(by_resource["auth.users"]["verbs"]) == {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }
    assert by_resource["auth.users"]["policy_class"] == "auth"
    assert by_resource["auth.users"]["grantable"] is True

    assert by_resource["heart.tick"]["policy_class"] == "ordinary"

    # Issue #38 AC: `me.profile` is marked non-grantable — every builtin role already
    # holds every verb there, so offering it in a custom-role picker would
    # communicate an escalation that doesn't exist.
    assert by_resource["me.profile"]["verbs"] == ["GET"]
    assert by_resource["me.profile"]["policy_class"] == "self"
    assert by_resource["me.profile"]["grantable"] is False

    # Issue #48: `me.password` is a `me.*` self-resource like every other one above —
    # served by exactly `PUT`, non-grantable for the same reason.
    assert by_resource["me.password"]["verbs"] == ["PUT"]
    assert by_resource["me.password"]["policy_class"] == "self"
    assert by_resource["me.password"]["grantable"] is False
