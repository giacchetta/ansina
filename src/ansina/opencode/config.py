"""Renders OpenCode's own `opencode.json` shape from Ansina's typed `[opencode]`
settings. See issue #74; the `provider` block below is issue #14's own small
additive change.

Rendered fresh on every preflight/spawn and embedded into `OPENCODE_CONFIG_CONTENT`
(`ansina.opencode.env.build_opencode_env`) — never written to a project-level
`opencode.json` file on disk. That was this module's original shape (`write_
opencode_config`, since removed) until issue #14's own real-hardware verification
found a second, more serious config-merge bug beyond the one `docs/opencode.md`'s
"config-merge finding" already named: with *both* a project `opencode.json` file and
`OPENCODE_CONFIG_CONTENT` present — even carrying byte-identical permission content —
real `edit`/`write` tool calls were denied outright, while the identical content via
`OPENCODE_CONFIG_CONTENT` *alone* (no project file at all) worked correctly. Confirmed
by a controlled A/B test against the real Ansina workspace directory and OpenCode's
own shared background service: renaming the on-disk file away (env unchanged) made
the exact same write succeed. Root cause not pinned down further (OpenCode's own
project-config-plus-`OPENCODE_CONFIG_CONTENT` merge path, not Ansina's rendering, is
the suspect) — the fix taken here is the structurally safe one: never let the
project-file source exist at all, so there is nothing to merge badly with.
`OPENCODE_CONFIG_CONTENT` alone already ranks above project config in OpenCode's own
documented precedence (see `docs/opencode.md`), so this loses no coverage.

Emission order inside `permission` is load-bearing, not cosmetic: OpenCode's
own documented precedence for `bash`/`edit`'s glob-map values is "rules are
evaluated by pattern match, with the last matching rule winning" — so `"*"`
(the deny floor) is always emitted first, then every allowlist glob, then
every denylist glob *last*, so an overlapping denylist entry always beats an
allowlist one rather than the reverse. `render_opencode_config` returns a
plain `dict`, so this order survives into `OPENCODE_CONFIG_CONTENT` via
`json.dumps`'s own default `sort_keys=False` — a sorted re-ordering would
silently change which glob rule wins.

`_render_provider()` (issue #14) is the custom-provider block for OpenCode's
own driving model, when `[opencode] provider_base_url` is set — i.e. when that
model is a self-hosted/custom OpenAI-compatible endpoint (a LiteLLM gateway, in
the first real deployment of this) rather than one of OpenCode's own built-in
providers. Verified against the real opencode.ai docs (`/docs/providers`'s own
"Custom provider" section) rather than assumed: a custom OpenAI-compatible
provider is `provider.<id>.npm = "@ai-sdk/openai-compatible"` plus
`options.baseURL`/`options.apiKey`, and `options.apiKey` supports a documented
`"{env:VARNAME}"` templating form that lets the *value* stay in the child
process's own environment — this function only ever emits the env var *name*
(`[opencode] provider_api_key_env`), never `provider_api_key`'s actual secret
value, so the rendered config is safe to embed verbatim into
`OPENCODE_CONFIG_CONTENT` (`ansina.opencode.env`). `_CUSTOM_PROVIDER_ID` is a
fixed constant, not a setting — Ansina configures at most one custom provider
for OpenCode's own model, so a second knob to name it would add no real
flexibility. `_render_provider()` also derives a `models.<model-id>` entry from
`[opencode] model` (stripping the `"ansina/"` prefix and any `#variant` suffix)
— found genuinely required by real-hardware verification against a real v2.2.1
binary and a real LiteLLM gateway, not assumed: `opencode run --model
ansina/<model-id>` against a provider with no matching `models` entry fails at
admission with `{"type":"provider.no-route","message":"Model unavailable: ..."}`
before ever reaching the network — the docs' own worked example already showed
one, but described it only as setting a display name/context limits, which
undersold how load-bearing it actually is.
"""

from __future__ import annotations

from typing import Any

from ansina.config.settings import Settings

_SCHEMA_URL = "https://opencode.ai/config.json"
_CUSTOM_PROVIDER_ID = "ansina"


def _render_glob_permission(
    *, allowlist: tuple[str, ...], denylist: tuple[str, ...]
) -> dict[str, str]:
    """`"*": "deny"` first (the floor), then every allowlist glob, then every
    denylist glob *last* — see module docstring for why this order is the
    contract, not an incidental detail of dict insertion order.
    """
    rendered: dict[str, str] = {"*": "deny"}
    for glob in allowlist:
        rendered[glob] = "allow"
    for glob in denylist:
        rendered[glob] = "deny"
    return rendered


def _render_edit_permission(settings: Settings) -> str | dict[str, str]:
    """`"workspace"` confines edits to the directory the child was actually
    spawned in — enforced by a bare, cwd-*relative* `"**"` pattern, not an
    absolute, `workspace_dir`-prefixed one.

    Found necessary by real-hardware verification, not assumed: against a
    real v2.2.1 binary, an absolute pattern (`f"{workspace_dir}/*"`, this
    function's own shape before this fix) never matched a single real
    `write`/`edit` tool call — not because of the glob syntax (`*` vs `**`),
    but because OpenCode's own documented convention for `edit` patterns is
    relative to the project root (confirmed against its own docs' worked
    example, `"packages/web/src/content/docs/*.mdx"`, no leading `/`), and in
    practice the tool's own `path` input varies between absolute and relative
    across calls — an absolute-rooted pattern can never match a relative
    input, or vice versa. A bare `"**"` is both correct and sufficient: `cwd`
    is always `opencode.workspace_dir` for every real spawn
    (`ansina.opencode.runner.OpencodeCliRunner._run_one`), so "everything
    reachable via a relative path from here" already *is* "everything inside
    the workspace." `edit` is also confirmed (both from OpenCode's own docs
    and from a real `"Permission denied: edit"` error on a real `write` tool
    call) to be the one action covering `edit`/`write`/`patch` together — no
    separate `write` key is needed.

    A `"**"` pattern does **not** reopen directory-escape risk: a relative
    path containing `../` segments is caught by OpenCode's own independent
    `external_directory` check (always `"deny"` here unless the operator
    explicitly widens it) *before* the `edit` glob is ever consulted —
    verified directly with a real `../../../../../../tmp/...` write attempt
    under this exact `"**"` rule, which failed with
    `"Permission denied: external_directory"`, never reaching `edit` at all.
    """
    opencode = settings.opencode
    mode = opencode.permission.edit
    if mode in ("deny", "allow"):
        return mode
    return {"*": "deny", "**": "allow"}


def _render_bash_permission(settings: Settings) -> str | dict[str, str]:
    permission = settings.opencode.permission
    mode = permission.bash
    if mode in ("deny", "allow"):
        return mode
    # "allowlist": the deny floor, then the configured allow/deny globs.
    return _render_glob_permission(
        allowlist=permission.bash_allowlist, denylist=permission.bash_denylist
    )


def _render_provider(settings: Settings) -> dict[str, Any] | None:
    """The custom-provider block for OpenCode's own driving model, or `None`
    when `[opencode] provider_base_url` is unset — see module docstring for the
    documented shape this mirrors and why `apiKey` never carries a real secret.

    Includes a `models.<model-id>` entry when `[opencode] model` names a model
    under this provider (`"ansina/<model-id>"`, optionally `#<variant>`) — found
    necessary by real-hardware verification, not assumed: `opencode run
    --model ansina/<model-id>` against a provider with no matching `models`
    entry fails at admission with `provider.no-route` ("Model unavailable"),
    never reaching the network at all. The docs' own worked example already
    showed one, but only ever described it as naming a display name/context
    limits — this is the empirical correction to that reading.
    """
    opencode = settings.opencode
    if not opencode.provider_base_url:
        return None
    options: dict[str, str] = {"baseURL": opencode.provider_base_url}
    if opencode.provider_api_key_env:
        options["apiKey"] = f"{{env:{opencode.provider_api_key_env}}}"
    provider_block: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Ansina",
        "options": options,
    }
    prefix = f"{_CUSTOM_PROVIDER_ID}/"
    if opencode.model.startswith(prefix):
        model_id = opencode.model[len(prefix) :].split("#", 1)[0]
        if model_id:
            provider_block["models"] = {model_id: {}}
    return {_CUSTOM_PROVIDER_ID: provider_block}


def render_opencode_config(settings: Settings) -> dict[str, Any]:
    """The pure, deterministic rendering `ansina.opencode.env.build_opencode_env`
    embeds into `OPENCODE_CONFIG_CONTENT` and `ansina.opencode.preflight.preflight`
    round-trips through `json.dumps`/`json.loads` as its own defense-in-depth check —
    never written to a project-level `opencode.json` file (see module docstring for
    why). Returns a plain `dict` so the unit suite can assert its shape (key order,
    no `"ask"` anywhere) with no filesystem involved.
    """
    permission = settings.opencode.permission
    rendered_permission: dict[str, Any] = {
        "*": permission.default,
        "read": permission.read,
        "edit": _render_edit_permission(settings),
        "bash": _render_bash_permission(settings),
        "webfetch": permission.webfetch,
        "websearch": permission.websearch,
        "external_directory": permission.external_directory,
    }
    # Last, deliberately: an operator-named key here can reach any other
    # OpenCode permission key this model doesn't name explicitly (see
    # `OpencodePermissionSettings.additional`'s own docstring) — appended
    # after, never merged ahead of, the keys this module controls directly.
    rendered_permission.update(permission.additional)

    rendered: dict[str, Any] = {"$schema": _SCHEMA_URL}
    provider = _render_provider(settings)
    if provider is not None:
        rendered["provider"] = provider
    rendered["permission"] = rendered_permission
    return rendered
