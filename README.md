# Scotty deployment

This repository builds one white-labelable managed wholesaling assistant for one maintainer and two client users, each of whom gets their own private channel, session, assistant name, Google Workspace account, and — where they supply one — their own Trello, GoHighLevel and RentCast credentials, falling back to the deployment's single shared business identity with the acting user still recorded. "Scotty" is the main operator's assistant, not the product; see `docs/white-label-rename-plan.md` for the post-acceptance rename. The initial add-ons are Discord, Trello, GoHighLevel, and RentCast. This repository prepares a stopped container from an immutable Hermes Agent 0.20.6 image and installs the generic bounded plugin into its private data mount.

The implementation contract is `docs/scotty-basic-release-engineering-contract.md`. The package does not include the future wholesaling application, student access, billing, webhooks, public ports, browser automation, scraping, Twilio, or commercial terms.

## Security boundary

The installer creates `/srv/Scotty/data` as the only container mount and keeps root-owned Compose/operator material outside that mount. The container has no published ports, container-engine socket, devices, privileged mode, or automatic restart. It uses `no-new-privileges`, 2 CPUs, 4 GiB memory, 512 PIDs, a private cgroup namespace, and the external `scotty-egress` bridge (`172.30.50.0/24`). The first-priority `DOCKER-USER` guard rejects host, private, Tailnet, metadata/link-local, multicast, documentation, benchmarking, and reserved IPv4 destinations while permitting public-internet egress.

The official image initializes through s6 as root and drops gateway work to UID/GID 10000. There is intentionally no Compose `user` override.

The assistant adds these enforced boundaries:

- Exact pre-model authorization matches `(guild_id, channel_id, user_id, role)` as one tuple. Mixed allowlist cross-products fail closed.
- A thread is accepted only under the configured principal channel.
- The gateway serves exactly three native profile routes: one full profile for the private route channel and one bounded profile per client channel. Each profile has its own home; the bounded plugin is staged only in the two client homes.
- The gateway admits only the three configured Discord senders, from a deterministic `DISCORD_ALLOWED_USERS` allowlist. That is admission only: a pre-dispatch gate in every profile additionally binds the acting user, which native routing does not match, and rejects every mixed tuple before any session or model activity.
- The full profile carries `scotty-guard`, which registers one pre-dispatch hook and nothing else: no model tools, no prompt section, no bounded client identity. The two client profiles carry the bounded plugin instead.
- Every served profile restates the provider and model chosen during setup, so a routed turn cannot silently fall back to a runtime default.
- The base configuration is bounded, so a profile widens its own surface only by overriding it. A profile whose override fails to apply is bounded, never unbounded.
- Client-visible Discord destinations are limited to the configured principal and announcement channels by construction.
- Native Discord slash commands and automatic threads are disabled.
- Each Discord channel retains its own gateway session; provider resources are the only shared business truth.
- The model inventory contains only `scotty_read`, `scotty_propose`, `scotty_approval`, `scotty_reminder`, and `scotty_calculate`.
- Hermes Tool Search is explicitly disabled so those tools are not replaced by a generic tool-discovery bridge.
- Approval and reminder state use owner-only SQLite files below `/opt/data/scotty`.
- Plugin-owned reminder polling does not register or expose native arbitrary cron.
- Provider redirects and automatic mutation retries are disabled. Ambiguous outcomes require reconciliation.
- Credentials are accepted only by the local hidden-input setup command or an exported environment variable, are stored in `/srv/Scotty/data/.env`, and never reach `argv`, stdout, logs, or public configuration. The model authenticates through the runtime's own `openai-codex` OAuth flow, which Scotty never sees, stores, or logs.
- Only the Discord bot token is required on day one. Trello, GoHighLevel and RentCast connect later; no placeholder is ever recorded as a connection.
- Private channels are created only after a local preview and confirmation, deny `View Channel` to `@everyone`, and are read back before they are recorded. An unconfirmable create is recorded as unknown and never resolved by creating a second channel.
- An unconfigured provider reports `not connected` with fixed guidance instead of taking the assistant down, and never asks for a credential in ordinary chat.
- Google Workspace is authorized for one configured account through Google's own browser consent. Ordinary reversible Gmail, Calendar, Drive, Docs, Sheets, and Contacts work runs without repeated approval; exact sends, new audiences, permanent deletion, sharing and permission changes, admin, account-security and billing actions, and bulk mutation are consequence-gated in code, and an unknown operation fails closed. The access token refreshes from owner-only state, so consent is completed once.
- Scotty is an ordinary assistant inside the configured guild, not only a setup wizard. Typed operations cover reading configured channels, sending, replying, editing and deleting only its own messages, reacting, attaching approved files from its own outbox, and running task threads. Streamed work updates keep one status message and coalesce edits behind a minimum interval and a hard write budget, so a long task reports progress without flooding the channel.
- Each client user reaches only their own private channel and the configured shared destinations. Neither can read, post into, or cross-route into the other's private channel or session, by any route including an approval, and the private maintainer route is unreachable from either.
- Publishing to a shared destination is approval-bound and is refused outright if the text carries a private channel or user identifier, maintainer route details, or anything credential shaped. Bulk messaging and unusually large mention lists are approval-bound too.
- Channel creation and deletion, role and permission changes, moderation, webhooks, bot installation, and destructive history cleanup have no operation at all: they fail closed rather than waiting for an approval, and there is no generic REST path to reach them by. Mentions are never parsed, so Scotty cannot ping a role or a server.
- Setup is a guided conversation from the authorized private channel: what each integration enables, the provider-console steps, the APIs to enable, the scopes, the identifiers, the callback behaviour, what is still missing, a specific correction for a malformed identifier, a named diagnosis for a provider failure, and the first unfinished step to resume at.
- Scotty never accepts a credential through Discord. Intercepting one before the event exists needs the pinned runtime's earliest raw-message boundary; `pre_gateway_dispatch` receives an already-constructed event, and that earlier boundary has not been inspected and attested here, so the capability is switched off rather than approximated. An intake phrase gets a specific refusal naming the local hidden-input setup command, and credential-shaped messages are stopped by the ingress leak scan before model dispatch. The one-time intake mechanism stays in the tree, inert and tested, behind a single source constant that no environment variable or configuration can flip.
- Credentials that do reach the server are held by a root-owned, fixed-operation broker outside the model-visible runtime, which validates and stores them and never returns them.
- Scotty can inspect its own health and perform exactly three bounded repairs on state it owns. It has no shell, package, Docker, systemd, firewall, host, or cross-client access, and a privileged need stops at a redacted diagnosis naming the one root-owned recovery command.
- `sudo /usr/local/sbin/scotty-start` is the single root-only lifecycle command. It validates the prepared stopped container, runs owner-only setup, completes the runtime's own Codex OAuth, validates Google consent against the configured account and scopes, starts only the prepared container, runs non-sending checks, and on any partial failure leaves Scotty stopped with one recovery instruction. It never sends onboarding.

A prompt, folder name, model, or persona is not a security boundary. The code, exact runtime configuration, container restrictions, network guard, and provider credential scopes collectively form the boundary.

## Architecture

- `assistant/scotty_business/`: installable Hermes plugin and bounded business domain.
- `assistant/scotty_business/ingress.py`: exact Discord tuple gate and fixed pre-model paths.
- `assistant/scotty_business/routing.py`: the native profile-routing contract plus the plugin's own fail-closed pre-dispatch tuple gate.
- `assistant/scotty_business/wizard.py`: single-delivery dispatch for the fixed onboarding wizard.
- `assistant/scotty_guard/`: self-contained pre-dispatch authorization for the full profile. No tools, no prompt, no client identity.
- `assistant/scotty_business/provisioning.py`: idempotent private-channel creation or reuse with preview, confirmation, and readback.
- `assistant/scotty_business/guidance.py`: fixed, credential-free provider setup guidance.
- `assistant/scotty_business/setup_flow.py`: guided setup progress, identifier validation, failure diagnosis, resume, and owner-only staging of non-secret identifiers.
- `assistant/scotty_business/credential_intake.py`: one-time protected credential intake ahead of every model-visible path, with confirmed source deletion and a privilege-separated broker.
- `assistant/scotty_business/google_policy.py`: code-enforced routine, consequence, and forbidden classification for every Workspace action.
- `assistant/scotty_business/google_oauth.py`: installed-app browser consent, account binding, and owner-only token refresh.
- `assistant/scotty_business/self_repair.py`: bounded health inspection and three narrow repairs over Scotty-owned state only.
- `assistant/scotty_business/discord_policy.py`: per-caller destination scope and routine/consequence/forbidden classification for every Discord action, plus the announcement leak check.
- `assistant/scotty_business/progress.py`: coalesced, rate-limited task status updates with hard write budgets and no blind retry.
- `assistant/scotty_business/approvals.py`: SQLite proposal state machine with `BEGIN IMMEDIATE`, version compare-and-set, immutable fields, nonce claims, crash recovery, and reconciliation states.
- `assistant/scotty_business/reminders.py`: tuple-scoped private reminders with atomic claims and no ambiguous retry.
- `assistant/scotty_business/adapters/`: typed, versioned Discord, Trello, GoHighLevel v3, and RentCast v1 adapters over a bounded standard-library HTTP transport.
- `assistant/scotty_business/service.py`: provider-independent proposal and execution workflows.
- `assistant/scotty_business/setup.py`: root-only hidden-input setup, Discord privacy/membership validation, and atomic private-state publication.
- `fixtures/`: synthetic configuration and provider data only.
- `install.sh`: fail-closed transaction that stages the package and creates, but never starts, the container.

Provider reference authorities:

- Trello cards and boards: https://developer.atlassian.com/cloud/trello/rest/api-group-cards/ and https://developer.atlassian.com/cloud/trello/rest/api-group-boards/
- GoHighLevel conversations v3: https://marketplace.gohighlevel.com/docs/ghl/conversations/send-a-new-message and https://marketplace.gohighlevel.com/docs/ghl/conversations/get-messages
- RentCast property data and AVMs: https://developers.rentcast.io/reference/property-data and https://developers.rentcast.io/reference/property-valuation
- Discord channels/messages: https://discord.com/developers/docs/resources/channel
- Google Workspace APIs: https://developers.google.com/workspace and https://developers.google.com/identity/protocols/oauth2/native-app

## Verify

Run the governing local gate from the repository root:

```sh
make verify
```

Individual gates:

```sh
make format-check
make lint
make typecheck
make test
make acceptance
make package
make smoke
make scan
make checksums
```

`make acceptance` runs a credential-free synthetic acceptance pass over the
fixtures. `make oauth-probe` reads the pinned image's own login subcommand from
a disposable, network-disabled container; it is not part of `make verify`
because it needs the pinned image present.

`make smoke` uses a disposable, network-disabled container from the pinned image and proves Hermes 0.20.6 discovers exactly the five Scotty tools, and that the generated configuration parses into three native profile routes with the served allowlist and per-profile toolsets it expects. It also drives the runtime's own sender authorization, the full admit/deny tuple matrix through both pre-dispatch hooks, the fixed wizard dispatch, and profile-scoped model resolution. It does not use credentials or providers. `make package` writes ignored deterministic artifacts below `dist/`.

## Install without activation

Run once from a clean, checksum-verified checkout on the documented Debian host:

```sh
sudo ./install.sh
```

The installer performs all preflight checks before mutation, installs the generic plugin and local setup command, creates the root-owned operator files, activates the host egress guard, creates the bridge, and creates the container. It never starts the container. Any error, interrupt, termination, or uncommitted exit triggers cleanup limited to objects created by that invocation.

The install fails closed if `/srv/Scotty`, the container, bridge, firewall chain/jump, installed operator files, or systemd unit already exists. There is no persistent rollback or uninstall script.

## Local private setup

After installation, while the container is still stopped, run:

```sh
sudo /srv/Scotty/operator/setup-scotty
```

The command:

1. verifies the `scotty` container exists and remains stopped;
2. reads every credential with hidden terminal input;
3. verifies the Discord bot identity and guild membership;
4. offers to create the main-operator and employee private channels, previewing each one and waiting for an explicit local confirmation, or accepts existing channel IDs;
5. verifies every configured Discord channel belongs to the exact guild and denies `View Channel` to `@everyone`, directly or through its category;
6. reads the private route back from Discord and rejects a nonexistent, public, cross-guild, inaccessible, or permission-drifted route;
7. creates or idempotently verifies one home per served profile and fails closed rather than falling back to the default profile;
8. writes `config.yaml`, `.env`, `scotty/private.json`, and one `profiles/<profile>/config.yaml` atomically with mode `0600` and UID/GID 10000;
9. leaves the container stopped.

The application needs `Manage Channels` plus normal messaging permissions.
Discord `Administrator` is never required.

If a credential is posted in Discord, do not reuse it. Rotate it and rerun local setup with the replacement.

## Capabilities and approvals

Reads are restricted to configured provider resources. Trello writes, GoHighLevel SMS, and Discord announcements require immutable proposals and the exact configured approver. Employees may propose but cannot approve or execute.

Legal approval states are:

```text
proposed -> approved | denied | expired
approved -> executing | expired
executing -> verified | failed | unknown
unknown -> verified | failed after authoritative reconciliation
```

An executing proposal becomes unknown after a restart. A timeout, malformed acknowledgement, or failed readback is unknown. It never returns to approved automatically and is never blindly retried.

Trello merge execution requires an exact normalized address or exact provider/property identifier. The proposal includes source fields, destination fields, conflicts, payload hash, and revisions. Scotty updates and reads back the destination before archiving and reading back the duplicate.

RentCast is read-only. Provider records retain endpoint, source ID, retrieval time, revision, source fields, and missing attributes. Deterministic calculations use `Decimal`; every result is preliminary and requires qualified-professional verification.

## Activation boundary

The final install and configure commands, the Codex OAuth step, the
credential-free acceptance command, and the maintainer, operator, and employee
acceptance prompts are in `docs/scotty-basic-release-commands.md`.

This repository does not activate Scotty. Starting the container, validating real provider scopes, performing live reads, sending a harmless acceptance write, changing systemd/firewall/runtime state, publishing a branch, opening a pull request, merging, or releasing requires a separate approved operations phase. See `docs/scotty-basic-operations.md` for the acceptance and rollback plan.
