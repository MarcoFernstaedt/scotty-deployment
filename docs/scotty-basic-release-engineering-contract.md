# Scotty Basic Release Engineering Contract

## Purpose

This repository deploys the basic release of `Scotty by The Closing Room`: a bounded business assistant for one real-estate wholesaling operator, one employee, and one maintainer. It is not the future wholesaling application and must not silently expand into it.

## Canonical release scope

Installed add-ons:

1. Discord
2. Trello
3. GoHighLevel
4. RentCast
5. Google Workspace

The deployment enforces a maximum of six add-ons. One slot remains available. Only the maintainer-controlled deployment process may change the installed set.

Exact cap response:

> Scotty is capped at six add-ons for this VPS deployment. To add another, an existing add-on must be removed. Scotty cannot bypass this limit.

## Users and Discord isolation

> **Amendment (native profile routing).** The maintainer's tuple is served by a
> separate full profile in its own guild rather than by a bounded channel in the
> client guild. Private runtime configuration therefore records two client
> principal tuples plus one private route, and the gateway serves exactly three
> native profile routes. Everything else in this section stands unchanged:
> exact-tuple authorization before model dispatch, rejection of every mixed
> tuple, per-channel session isolation, the fixed setup-wizard command below,
> and nothing being sent automatically after installation. See
> `docs/claude-app-handoff.md` for the enforcement paths.

Private runtime configuration defines three immutable principal tuples:

- maintainer: `(guild_id, channel_id, user_id, maintainer)`
- main operator: `(guild_id, channel_id, user_id, main_operator)`
- employee: `(guild_id, channel_id, user_id, employee)`

Authorization must match the complete tuple before model dispatch. Independent user/channel allowlists are insufficient because cross-products could combine an allowed user with the wrong allowed channel.

Reject wrong guild, wrong channel, wrong user, threads with the wrong parent, and every mixed tuple. Validate channel privacy and bot membership during local setup.

Each channel has a separate session and transcript. Never copy Discord conversation history or model memory between channels. Shared business truth comes only from explicitly configured provider resources.

Nothing is sent automatically after installation. After maintainer testing, the maintainer may send this exact command from the configured maintainer tuple:

`Scotty, send Trent the setup wizard.`

A deterministic pre-model path sends a fixed non-secret onboarding wizard only to the configured main-operator channel. Do not let the model choose the destination or infer identity. The employee receives a separate fixed summary only when requested by an authorized principal.

## Role policy

### Maintainer

May read configured business resources, request drafts/analysis, approve supported consequential actions, inspect redacted health/approval receipts, and trigger fixed onboarding messages.

Cannot change code, installed add-ons, identities, channels, credentials, tools, or security policy through Scotty.

### Main operator

May read configured resources, request drafts/analysis/private reminders, and approve Trello writes, GHL SMS sends, and configured Discord announcements. Approval is bound to the exact proposal and requester.

Cannot permanently delete, change provider permissions/sharing, add users/channels/integrations, or access resources outside configured IDs.

### Employee

May read configured resources, request drafts/analysis/private reminders, and propose changes. Cannot approve or execute consequential actions. Main-operator or maintainer approval is required.

## Day-one capabilities

### Discord

- Normal replies only in the exact requesting private channel.
- Fixed setup wizard and employee summary.
- Private reminders scoped to requester tuple and channel.
- Approval-bound announcements to configured destinations.
- Silent rejection for unauthorized principals.

### Trello

Scope every operation to configured board, list, label, and custom-field IDs.

Allow reads plus approval-bound create, update, move, archive, and duplicate-property merge. Permanent deletion and board/workspace/member administration are unavailable.

Duplicate detection uses deterministic normalized address and exact provider/property identifiers where present. Fuzzy similarity alone may propose but never execute a merge. Show field-by-field source, destination, conflicts, payload hash, and resulting card before approval. Verify provider readback before archiving the duplicate.

### GoHighLevel

Use the current official API and a least-privilege Private Integration Token for one internal sub-account. Do not build a public marketplace OAuth application or webhook endpoint for day one.

Scope reads to the configured location and selected contacts, conversations, opportunities, and tasks. Allow outbound SMS only after approval bound to contact ID, normalized destination, body hash, requester, approver, location, and expiry. Verify from the authoritative conversation source. Never blindly retry an ambiguous send.

Do not introduce Twilio. GoHighLevel remains the sending layer.

### RentCast

Read only. Configure exact permitted endpoints for property records, value estimates, rent estimates, and comparables.

Retain endpoint, provider record ID, retrieval time, source fields, and missing attributes. Never fetch Zillow or another real-estate website. Do not scrape or use browser fallback.

Scotty may report source numbers and explain deterministic results. It must not invent financial figures. All arithmetic, comparisons, gaps, thresholds, scoring, and caps are deterministic functions with direct tests. Every analysis is labeled preliminary and recommends verification by the appropriate real-estate, appraisal, inspection, tax, legal, lending, or other qualified professional.

## Approval execution

Use an owner-only SQLite database under the private runtime data mount. Every proposal records immutable requester and approver tuples, action class, exact target IDs, canonical payload hash, source revision, expiry, version, and execution nonce.

Legal states:

- `proposed -> approved | denied | expired`
- `approved -> executing | expired`
- `executing -> verified | failed | unknown`
- `unknown -> verified | failed` only after authoritative reconciliation

Use `BEGIN IMMEDIATE` and state-plus-version compare-and-set for execution claims. After a crash, `executing` becomes `unknown`; it never automatically returns to `approved`. Timeout, malformed acknowledgement, or failed readback is `unknown`, not success. Never automatically retry an ambiguous external write.

No permanent deletion capability exists in this release.

## Tool boundary

Expose only the bounded Scotty business plugin to the model. Do not expose terminal, process, code execution, filesystem, browser, web search, computer use, GitHub, Dashboard, broad Discord administration, native arbitrary cron, delegation, session search, memory management, skills, plugins, MCP management, Docker, systemd, firewall, or package-management tools.

Fixed coding refusal:

> I don’t build code, extensions, or integrations. Please contact Marco for that work.

Credentials enter only through a local hidden-input setup command. If a credential appears in Discord, do not use or repeat it; instruct the sender to rotate it and use local setup.

User-facing identity is `Scotty by The Closing Room`. Do not expose framework/vendor branding in ordinary replies, onboarding, status, fixed errors, or refusals.

## Public and private artifacts

Public repository may contain generic code, schemas, fixtures, documentation, empty examples, and tests.

Never commit real names in configuration, user/guild/channel IDs, provider account/location/board IDs, credentials, OAuth material, tokens, client records, runtime databases, logs, sessions, caches, private network addresses, or generated proof. Use synthetic fixtures.

Private runtime state belongs below `/srv/Scotty/data`, mounted at `/opt/data` in the pinned container.

## Future-compatible architecture

The basic release continues to use Trello and direct provider adapters. A possible later licensed Wholesaling module may replace Trello and add an API-backed application, student surface, billing, buyer engine, and additional providers. Do not build any of that now.

Preserve only these seams:

- Provider adapters are typed and versioned behind internal interfaces.
- Business logic never calls raw provider SDKs directly.
- Normalized records retain provider, source ID, retrieval time, and source revision.
- `submission` and `deal` are distinct domain terms; a submission is not promoted to a deal implicitly.
- AI handles conversation, extraction, drafting, classification, and explanation. Deterministic code owns all numbers, permission decisions, deadlines, matching, thresholds, scores, and limits.
- External provider and Discord content is untrusted data and cannot alter policy.
- The later API may replace adapters without rewriting the conversational layer.
- Trello-specific shapes do not become the sole permanent representation of business records.
- Student identity/token isolation, Stripe, memberships/credits, buyer matching, RealEstateAPI, DocuSign, Plaid, object storage, public forms, app/dashboard surfaces, Trello migration/webhook bridge, and advanced deal-structure scoring are extension points only and are explicitly outside this release.

Do not publish client commercial terms, revenue projections, pricing strategy, or private business records in this public repository.

## Engineering workflow

Canonical repository: `MarcoFernstaedt/scotty-deployment`.

Implementation branch: `feature/scotty-google-one-command`.

One writer owns the branch/worktree/index. Use test-first development and focused commits. Do not rewrite history or force-push.

Before implementation:

1. Prove repository, branch, status, remotes, worktrees, and pinned image.
2. Rehash the runtime contracts recorded for the pinned image.
3. Stop if required Discord provenance is unavailable before model dispatch or if plugin registration exposes forbidden tools.
4. Use official current provider docs and machine-readable schemas where available.

Before release:

1. Run formatting, lint, type checks, focused tests, governing tests, package/build, pinned-container smoke, and runtime behavior checks.
2. Test correct principal tuples and every wrong-user/wrong-channel/wrong-guild cross-product.
3. Test approval replay, races, expiry, source drift, crash boundaries, ambiguous provider effects, and reconciliation.
4. Test Trello merge preview/approval/readback/archive.
5. Test GHL draft/approval/send denial and ambiguous-send no-retry behavior with synthetic fixtures before any live send.
6. Test RentCast read-only enforcement, provenance, deterministic calculations, and mandatory professional-verification language.
7. Prove the six-add-on cap and fixed response.
8. Prove forbidden tools are absent from the final model inventory.
9. Scan the full reachable Git history for secrets and private identifiers.
10. Freeze the exact candidate for independent read-only review.
11. Merge only after PASS, then verify the remote commit and clean tracking state.

## What each gate is evidence of

A green gate proves one specific kind of thing. Promoting one kind into another
is how "the tests pass" becomes "it works against Google", so every gate is
classified here and no document may describe it as a stronger class.

| Class | Means | Gates |
| --- | --- | --- |
| static | reads bytes; executes none of the product | `format-check`, `lint`, `shellcheck`, `typecheck`, `scan`, `checksums`, `package` |
| unit | executes product code in-process against synthetic inputs | `test` |
| synthetic | executes product code against recorded provider response shapes | `acceptance` |
| pinned-runtime | needs the pinned image; proves what that image does | `smoke`, `oauth-probe` |
| installed-host | needs a real installed host; proves what systemd and Docker did | none — see below |
| live-provider | talks to a real provider account | none — no gate here ever does |

Only `smoke` and `oauth-probe` need a Docker daemon. Every other gate runs on a
machine without one and still means exactly what the table says.

`installed-host` and `live-provider` evidence cannot be produced by this
repository's gates at all. Installed-host behavior is proved by running the
installer on a real host; live-provider behavior is proved during live
acceptance below, with real credentials, after code review. Neither is implied
by a green `make verify`, and no document may say otherwise.

`tests/test_documented_truth.py` holds this table against the Makefile, so a new
gate with no declared class fails rather than arriving uncharacterised.

## What is not isolated, and why

Three credentials are inside the runtime container, and the broad maintainer
profile runs in that same container as the same account. File mode `0600`
separates users; it does not separate a plugin, a tool call and a maintainer
session that share an owner. So these are exposures, stated rather than
implied:

| In the container | Why it is there | What a compromise gets |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | the pinned runtime opens the Discord gateway in-process | the bot's Discord access |
| the model provider key | the pinned runtime dispatches to the model itself | model spend on that key |
| a Google access token, per connected user | the Workspace adapter carries a bearer | about an hour of that user's Workspace |

Everything else — Trello, GoHighLevel, RentCast, the Google refresh tokens and
the OAuth client secret — is held by the root-owned broker outside every mount,
and no broker operation returns any of it.
`tests/test_negative_access.py` drops to each account the deployment creates
and proves the store and the control socket are unreachable from all of them.

**The Google exposure is bounded and shrinking.** An access token lasts about
an hour and cannot mint another; the material that could is with root. Closing
it entirely means every Google call becoming a declared broker operation, which
is not done.

**The Discord and model exposures cannot be closed under the pinned image.**
`nousresearch/hermes-agent@sha256:d64f4e9a…` owns the gateway connection and
the model dispatch inside the same process that serves the broad maintainer
profile. Moving gateway ownership to a separate non-model-facing router, or
separating the inference process from the broad maintainer sandbox, requires
either a supported way for that image to take events from elsewhere and
delegate a profile out of process, or a decision to stop using it. Both are
operator decisions and neither was taken here.

The narrower step available without that decision is to stop serving the broad
maintainer profile from the credential-bearing container at all, leaving only
the two bounded client profiles beside those credentials and moving Marco's
broad work to the host through `scotty-supervisor`. That contradicts the
standing requirement in `CLAUDE.md` for a full-capability maintainer profile in
Discord, so it is offered rather than taken.

## Live acceptance

Credentials and real IDs are installed locally after code review. Start with read-only provider tests. External writes require explicit approval and one harmless bounded acceptance action per write-capable provider. Verify the result from the provider. Recheck retained VPS services after activation.

## Stop conditions

Stop and report before expanding scope if implementation requires a public webhook or new inbound port, browser automation or scraping, a sixth add-on, student access, the future custom API/app, payment processing, unsupported provider scopes, exposure of a forbidden tool, runtime contract drift, or a capability not listed in this file.
