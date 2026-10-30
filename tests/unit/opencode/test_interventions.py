from __future__ import annotations

from ansina.opencode.interventions import (
    OpencodeAction,
    build_nudge_request,
    build_retry_request,
)
from ansina.opencode.repository import OpencodeRun


def _run(
    *,
    run_id: str = "run-1",
    prompt: str = "do the thing",
    session_id: str | None = "session-1",
    model: str | None = "gpt-test",
    agent: str | None = "build",
) -> OpencodeRun:
    return OpencodeRun(
        id=run_id,
        created_at="2026-01-01T00:00:00.000Z",
        started_at="2026-01-01T00:00:01.000Z",
        completed_at=None,
        status="failed",
        session_id=session_id,
        prompt=prompt,
        model=model,
        agent=agent,
        exit_code=1,
        error_detail="boom",
        event_count=3,
        tool_call_count=0,
        permission_denied_count=0,
        parse_error_count=0,
        prompt_tokens=None,
        completion_tokens=None,
        duration_seconds=1.5,
        retry_of=None,
        intervention_count=0,
    )


def test_opencode_action_is_the_closed_five_member_set() -> None:
    assert {member.value for member in OpencodeAction} == {
        "retry",
        "nudge",
        "compact",
        "pause_intake",
        "escalate_to_brain",
    }


def test_build_retry_request_reuses_prompt_model_agent_with_no_session() -> None:
    run = _run(prompt="fix the bug", model="gpt-test", agent="build")

    request = build_retry_request(run)

    assert request.prompt == "fix the bug"
    assert request.model == "gpt-test"
    assert request.agent == "build"
    assert request.session_id is None
    assert request.continue_last is False
    assert request.fork is False


def test_build_nudge_request_uses_a_fixed_continuation_and_the_runs_session() -> None:
    run = _run(prompt="a prompt the model never sees again", session_id="session-42")

    request = build_nudge_request(run)

    assert request.session_id == "session-42"
    assert request.prompt != run.prompt
    # The continuation is fixed and code-generated — never derived from the
    # run's own prompt content.
    assert "continue" in request.prompt.lower()


def test_build_nudge_request_is_deterministic_regardless_of_run_content() -> None:
    """Structural safety test (issue #79's own acceptance criterion): the nudge
    prompt never varies with the run's own prompt — only `session_id`/`model`/
    `agent` do.
    """
    run_a = _run(run_id="a", prompt="alpha")
    run_b = _run(run_id="b", prompt="totally different beta content")

    assert build_nudge_request(run_a).prompt == build_nudge_request(run_b).prompt


def test_build_retry_request_never_accepts_a_free_text_parameter() -> None:
    """Both builders take exactly one positional parameter — an already-
    persisted `OpencodeRun` — and nothing resembling a raw string the Heart's
    own model output could ever be threaded through.
    """
    import inspect

    retry_params = list(inspect.signature(build_retry_request).parameters)
    nudge_params = list(inspect.signature(build_nudge_request).parameters)
    assert retry_params == ["run"]
    assert nudge_params == ["run"]
