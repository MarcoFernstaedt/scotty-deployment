---
paths:
  - "assistant/scotty_business/adapters/**/*.py"
  - "assistant/scotty_business/{service,runtime,google_oauth,google_policy,policy,setup,setup_flow,guidance}.py"
  - "tests/{test_google_workspace,test_provider_connection,test_policy_and_calculations,test_setup,test_setup_flow}.py"
---

# Actor-bound provider and Google rules

## Per-user identity

Trent and Mikey complete separate setup and use separate provider credentials/API identities where supported.

- Never allow the model to select/override actor, account, token path, credential ID, OAuth client, refresh token, authorization code, provider identity, or tenant mapping.
- Resolve actor-to-credential mapping server-side from trusted Discord provenance.
- An unlinked actor gets deterministic setup guidance and zero provider access.
- Cross-user credential/account overrides fail before provider execution.
- Shared provider credentials are allowed only when required by the business/provider; bind every operation to the authenticated Discord actor and retain attribution.

## Google Workspace

- Separate OAuth record for Trent and Mikey.
- Each actor gets only their own Gmail, Calendar, Contacts, Drive, Docs, and Sheets, except resources explicitly shared by Google and permitted by policy.
- Use the proven headless OAuth flow: protected local Desktop-client import, exact visible authorization URL, browser consent by the correct user, safe full localhost redirect/code exchange, account binding, authentication and representative bounded-read verification.
- Do not use an unprinted `webbrowser.open()` loopback flow on the VPS.
- Document client creation, consent/test users, scopes, token permissions, refresh, revoke/reconnect, and expired-code recovery.
- Reads, searches, drafts, previews, reversible organization, and bounded edits are low-friction.
- Exact sends, destructive deletion, public sharing, permission changes, attendee-impacting actions, bulk actions, Admin SDK, billing, account-security, and credential changes require exact consequence controls.
- A mutation is `VERIFIED` only after operation-specific authoritative readback matches intended state. Malformed, unavailable, stale, partial, mismatched, or ambiguous outcomes become `UNKNOWN` and require reconciliation before retry.
- Add operation-aware thresholds for Docs/Sheets requests, ranges, cells, entries, recipients, files, and affected resources.
- Drive reads must support bounded file content/export with MIME and size checks. Sheets reads must support bounded `values.get` and `values.batchGet`.

## Trello identity

- Separate accounts/tokens for Trent and Mikey when supplied.
- Both may work on explicitly approved shared property boards/lists.
- Never permit one actor to select or use the other actor’s token.
- Attribute actions to the authenticated Discord actor even when a shared integration identity executes them.

## GHL, RentCast, and future providers

- GHL: prefer separate user/location credentials where supported. Shared business/location credentials still require actor-bound policy and attribution.
- RentCast: per-user keys when provided; otherwise approved shared read-only infrastructure with actor attribution.
- Future providers use actor-bound identity, least privilege, typed operations, approvals, idempotency, audit, and authoritative reconciliation.

## Guided setup

Each user receives resume-safe onboarding for their own integrations. Explain capabilities and current provider setup, collect only non-secret identifiers in ordinary chat, use protected/local secret intake, validate scopes without revealing values, report `connected`, `degraded`, `blocked`, `expired`, or `not configured`, generate reconnect guidance, and resume at the first unfinished step.