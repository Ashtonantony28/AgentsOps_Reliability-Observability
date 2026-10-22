# Platform contracts (SG-P0)

Frozen by SG-P0 as schemas, storage (migration `0007_platform_contracts`) and stub routes. SG-P1 implemented the
auth, webhook and OAuth parts (tests in `backend/tests/api/test_platform_security.py`), SG-P2 the evaluation runner. Changes go through the Rules line in `PLAN.md`.

## Invite tokens and password change

Schemas in `app/schemas/auth.py`, storage `invites` (`app/models/organization.py: Invite`), TS in
`frontend/src/types/auth.ts`.

| Method | Path | Auth | Body → Response | Now |
|---|---|---|---|---|
| POST | `/api/auth/invite` | jwt admin | `InviteRequest {email, role}` → `InviteOut` | SG-P1 |
| POST | `/api/auth/accept-invite` | the token in the body | `AcceptInviteRequest {token, password}` → `TokenPair` | SG-P1 |
| POST | `/api/auth/password` | jwt, any role | `PasswordChangeRequest {current_password, new_password}` → `TokenPair` | SG-P1 |

- `InviteOut = {email, role, status: "added" | "pending", invite_id, expires_at}`. It never carries a password or the
  token. `added`: the email already has an account, which is now a member with `role` (or has its role changed), and
  `invite_id`/`expires_at` are null. `pending`: no account; an `invites` row is stored and the token is sent to the
  email only (`notification_service.send_email`, logged in dry-run), as a link to the frontend's
  `/accept-invite?token=…` page (`FRONTEND_URL`, default `http://localhost:3000`).
- `role` is one of `viewer | developer | admin` (422 otherwise), so an admin cannot invite an owner; nor can an admin
  change an existing owner's role this way (403 `insufficient_role`; an owner can).
- Token: `secrets.token_urlsafe(32)`; `invites.token_hash` is its SHA-256 hex (unique). An invite expires 72 h after
  it is made and is used once (`accepted_at`). Inviting an email with a pending invite in the same org replaces it,
  so the old token stops working.
- `accept-invite`: creates the user with `password`, adds the membership, sets `accepted_at` and signs in to the
  invite's org (a session like login). 400 `invalid_invite` for an unknown, expired or used token; 409
  `email_exists` if an account with that email was made after the invite (the admin invites again and gets
  `added`). Password bounds as at registration (8 to 128); hashing runs off the event loop.
- `password`: checks `current_password` (400 `wrong_password`), stores the new hash, revokes every session of the
  user and returns a new `TokenPair` for the caller's org.

## Integration webhook signing

| Method | Path | Auth | Body → Response | Now |
|---|---|---|---|---|
| POST | `/api/integrations` | jwt admin | `IntegrationCreate` → `IntegrationCreatedOut` | SG-P1 |
| POST | `/api/integrations/{id}/signing-secret` | jwt admin | → `SigningSecretOut {signing_secret}` | SG-P1 |
| POST | `/api/integrations/webhooks/{kind}/{org_id}` | `X-AgentOps-Signature` | JSON object → `{ok: true}` | SG-P1 |

- The secret lives in `integrations.signing_secret` (`VARCHAR(64) NULL`): `secrets.token_hex(32)`, made for every
  integration on create and returned once, in `IntegrationCreatedOut.signing_secret`. The rotate route replaces it and
  returns the new one; the old one stops working at once. `IntegrationOut` (list, update) has no such field.
  Integrations made before SG-P1 have none until rotated.
- Signature: `X-AgentOps-Signature: sha256=<hex>`, the lowercase hex HMAC-SHA256 of the raw request body bytes,
  keyed by the secret's UTF-8 bytes. Example: secret `"a" * 64`, body `{"event":"test"}` →
  `sha256=668359ea785432c809ea605fdffed3351acabbb3b11c925f900366d28a1800dc`.
- The receiver checks the signature before parsing the body, in constant time, against each enabled integration of
  `kind` in `org_id` that has a secret. No match, a missing header, an unknown org or kind: all 401
  `invalid_signature`, so the answer does not say which orgs exist. A signed body that is not a JSON object is 422.
  The audit row records the matched integration's id. No timestamp or replay window in v1.

## Slack OAuth state

Storage `oauth_states` (`app/models/integration.py: OAuthState`): `id, provider, state_hash (unique), org_id,
user_id, created_at, expires_at, used_at?`.

- `GET /api/integrations/oauth/slack/start` (jwt admin) → `SlackOAuthStartOut {url}` (shape unchanged). From SG-P1
  the URL's `state` is `secrets.token_urlsafe(32)`, not the org id; the row stores its SHA-256 with the caller's org
  and user, and expires 10 minutes later.
- `GET /api/integrations/oauth/slack/callback?code&state` → `SlackOAuthCallbackOut {ok, team}`. Before exchanging the
  code it consumes the state in one statement (`UPDATE … SET used_at = now WHERE state_hash = :h AND provider =
  'slack' AND used_at IS NULL AND expires_at > now`); no row updated, or a starting user who is no longer an admin of
  that org, is 400 `invalid_state`. The integration is made in the row's org.

## Evaluation runner

`EvaluationSet.runner` (`evaluation_sets.runner JSON NULL`), a discriminated union on `type`
(`app/schemas/evaluation.py: EvalRunner`; TS `EvalRunner` in `frontend/src/types/evaluation.ts`). `EvalSetCreate`
and `EvalSetUpdate` take it (on update an explicit `null` removes it, an absent field keeps it) and `EvalSetOut`
returns it; this is stored now, and SG-P2 runs it.

| `type` | Fields | Output of a case |
|---|---|---|
| `webhook` | `url` (`http(s)://`, ≤ 2048), `timeout_s` (default 10, ≤ 60) | `POST url` with `{run_id, set_id, case_id, agent_id, agent_version, input}`; a 2xx JSON object with a string `output` |
| `traces` | `match`: `exact` (default) or `contains`, `lookback_days` (default 7, 1 to 90) | the `output` of the newest trace of the run's agent within `lookback_days` whose `input` equals the case input (`exact`, both stripped) or contains it (`contains`) |

- No runner (null), a webhook error (non-2xx, timeout, bad JSON, no string `output`), no matching trace, or an empty
  output: the case fails with score 0 and `reason` `no_runner`, `runner_error: …`, `no_matching_trace` or
  `no_output`, whatever the evaluators. A case never passes on the expected output standing in for the actual one.
- The webhook URL goes through SG-P1's outbound URL check (`app/services/outbound.py: post`: no private, loopback or
  link-local addresses; the request goes to the address that was checked). The request is unsigned in v1.
- Sets that exist before `0007` have no runner, so from SG-P2 on their runs fail every case until one is set.
