-- Migration 0008: in-flight OIDC login state (issue #43).
--
-- The authorization-code flow (`POST /auth/oidc/login` -> IdP -> `GET
-- /auth/oidc/callback`) spans two separate requests, potentially minutes apart and
-- possibly against a different worker process, so the `state`/`nonce`/PKCE
-- `code_verifier` triple issued at `/login` must survive somewhere durable rather than
-- an in-memory dict — the same reasoning `sudo_grants`/`sudo_lockouts` (0003_sudo.sql)
-- already follow for short-lived, caller-facing tokens.
--
-- `state` is the primary key and the value the callback looks itself up by — CSRF
-- protection for the redirect. `nonce` is echoed back inside the IdP's signed ID token
-- and compared there (`ansina.auth.oidc.validate_id_token`) to prove *this* login
-- produced *that* token, not a replayed one from elsewhere. `code_verifier` is this
-- login's PKCE secret (RFC 7636, S256) — generated at `/login` time alongside `state`/
-- `nonce`, sent to the IdP only as its S256 `code_challenge`, and presented in the
-- clear at the token-exchange step so a party that only intercepted the authorization
-- code (but never saw this table) cannot redeem it.
--
-- No foreign key: no `users` row exists yet when a row here is created — that's the
-- entire point of this table, holding pre-identity state.
--
-- `OidcLoginStateRepository.take()` deletes a row in the same transaction it reads it
-- in, making every row single-use: a replayed `state` value finds nothing the second
-- time, indistinguishable from one that never existed.
CREATE TABLE oidc_login_states (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL
);

-- Backs `OidcLoginStateRepository.delete_expired()` — a periodic sweep of rows nobody
-- ever came back to redeem (an abandoned browser tab, a crashed IdP round trip).
CREATE INDEX idx_oidc_login_states_expires_at ON oidc_login_states (expires_at);
