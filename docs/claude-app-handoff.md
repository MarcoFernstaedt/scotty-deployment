# Claude App Handoff: Finish Scotty Basic Release

## Repository and branch

- Repository: `MarcoFernstaedt/scotty-deployment`
- Working branch: `feature/scotty-basic-assistant`
- Verified starting commit: `cbc8c83`
- Base branch: `main`

Do not work directly on `main`. Do not force-push or rewrite history.

## Verified current state

At `cbc8c83`, the generic bounded business plugin, typed Trello/GoHighLevel/RentCast adapters, approval state machine, exact-principal ingress checks, private setup workflow, fixtures, package builder, installer staging, and pinned-runtime smoke tests exist.

The following command passed before handoff:

```bash
make verify
```

Evidence from that run:

- Ruff format and lint passed.
- mypy passed for 22 source files.
- 56 unit tests passed.
- ShellCheck passed.
- package archive checksum passed.
- pinned Hermes `0.20.6` smoke exposed exactly five bounded Scotty tools.
- repository scan covered 53 working files and 33 reachable historical blobs with no secret patterns.
- firewall cleanup, ERR-trap, and symlink-preflight tests passed.

Treat that as a baseline, not proof of the remaining requirements.

## Remaining objective

Finish the generic release without live credentials or external writes.

### 1. Private Discord channel provisioning

Extend the hidden local setup command to idempotently create or reuse two private text channels in the configured client guild:

- one main-operator channel;
- one employee channel.

Do not hard-code real guild, user, channel, or bot IDs in the repository.

Required behavior:

- Bot token is read only through hidden local input or process environment, never argv/stdout/logs.
- Verify bot identity and configured guild before mutation.
- Create channels only after an explicit local preview/confirmation.
- Deny `@everyone` `ViewChannel`.
- Grant the configured user and bot only the permissions required for chat.
- Prefer `Manage Channels` plus normal message permissions; do not require Discord `Administrator`.
- Reuse an existing channel only when its exact guild, intended user, and permission overwrites match; otherwise stop rather than hijack it.
- Read Discord back after creation and fail if privacy or membership differs.
- Record channel IDs only in owner-only private runtime config.
- Make reruns idempotent and prevent duplicate channels.
- Add synthetic REST fixtures and negative tests for wrong guild, wrong bot, name collision, partial creation, permission drift, 403, timeout, and ambiguous response.
- If channel creation may have occurred but readback is unavailable, mark unknown and never create another blindly.

### 2. Full maintainer profile, bounded client profiles

Use the pinned runtime’s native multiplex profile routing and per-source toolset resolution.

Desired behavior:

- One dedicated Discord bot identity can belong to both the client guild and a maintainer guild.
- Client guild/channel sources route to bounded Scotty profiles.
- The exact maintainer guild and private channel route to a separate full Hermes profile.
- The full maintainer profile retains normal protected-action approvals.
- Add an exact maintainer user-ID authorization check before model dispatch; native profile routing matches guild/channel but not user.
- Wrong user in the maintainer channel is silently rejected before session or model activity.
- Trent/Mikey profile state, prompts, memories, status, onboarding, tools, and errors contain no maintainer guild/channel/profile identifiers or statements that a hidden admin route exists.
- No home/proactive-delivery channel points at the maintainer route.
- No client-visible tool can send to the maintainer channel.
- Sessions, memories, and configuration remain profile-local; shared business data is accessed only through bounded provider records.
- Test exact routing, wrong-user cross-products, threads/parent channels, profile-state separation, bounded client tool inventory, full maintainer inventory, and absence of maintainer references in all fixed client-facing strings.

Relevant pinned-runtime contracts to inspect rather than assume:

- `gateway/profile_routing.py`
- `gateway/run.py::_resolve_enabled_toolsets_for_source`
- `gateway/platforms/base.py::toolsets_for_source`
- Discord `SessionSource` fields and thread parent behavior

Stop if the implementation would require widening client toolsets, embedding private IDs in public code, or relying on prompt-only secrecy.

### 3. Provider setup guidance

Add deterministic non-secret guidance for:

- Discord;
- Trello;
- GoHighLevel;
- RentCast;
- optional future Google Workspace.

When a provider is unconfigured, Scotty must state `not connected`, explain the provider-side steps and required IDs/scopes, and direct the operator to the hidden local setup command. It must never ask for a credential in Discord or accept one from chat.

Google Workspace is guidance only in this release; do not add a live Google adapter or consume another add-on slot without a later decision.

### 4. Final setup and acceptance handoff

Produce:

- one final install/configure command;
- the Codex OAuth step using the command supported by pinned Hermes `0.20.6`;
- one credential-free synthetic acceptance command;
- one concise prompt Marco can send through the maintainer channel to validate full-profile routing;
- one concise prompt for Trent’s bounded channel;
- one negative test for Mikey approval denial;
- the exact maintainer-triggered wizard command;
- clear `not connected` provider behavior.

Nothing may send automatically after installation. The setup wizard goes to the main operator only after the exact authorized maintainer command.

## Public/private boundary

Never commit:

- real Discord IDs;
- bot tokens;
- provider credentials;
- OAuth material;
- private guild/channel names;
- client records;
- runtime state, sessions, logs, databases, caches, or proof files;
- the private commercial/product overview or its financial terms.

Use synthetic 17–20 digit Discord snowflakes and fake provider records only.

## Required engineering gates

Use test-first changes and keep commits focused. Before opening a pull request, run:

```bash
make verify
git diff --check
git status --short --untracked-files=all
```

Also prove:

- no secret/private-ID patterns in the full reachable history;
- generated package matches its checksum;
- exact pinned-container plugin/runtime smoke;
- no Docker, systemd, firewall, `/srv/Scotty`, live Discord, or provider mutation during development;
- no new dependency unless justified and pinned;
- no generated cache/build artifacts committed.

Open a pull request to `main` only after all gates pass. Do not merge it. Return the PR URL, final commit, changed files, exact commands/results, remaining live-credential steps, and any blocker.

## Claude App prompt

Use this prompt after connecting Claude to the repository and selecting the feature branch:

> Continue the Scotty basic release from `docs/claude-app-handoff.md`. Read that file, `docs/scotty-basic-release-engineering-contract.md`, `README.md`, `Makefile`, `install.sh`, `compose.yaml`, the plugin package, and all tests before editing. Preserve the verified `cbc8c83` baseline. Implement only the remaining private-channel provisioning, native multiplexed full-maintainer routing, provider setup guidance, and final command/test handoff. Use test-first development, synthetic fixtures, no real credentials or IDs, no live external mutations, and run `make verify`. Commit focused changes to `feature/scotty-basic-assistant`, push, and open but do not merge a PR to `main`. Stop on any security or pinned-runtime incompatibility and report it rather than weakening the design.
