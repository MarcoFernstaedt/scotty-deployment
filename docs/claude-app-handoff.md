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

---

## Completion status

Everything in "Remaining objective" above is implemented on this branch, with
the two open items recorded at the end of this section.

### 1. Private Discord channel provisioning — done

`assistant/scotty_business/provisioning.py`, wired into `setup.py`.

- The bot token is read from hidden local input or an exported environment
  variable; `setup.py` contains no `argv` or `argparse` use, and the token lives
  only inside a mapping that refuses to render itself.
- Bot identity, guild identity, guild membership, and `Manage Channels` are all
  verified before any mutation. `Administrator` is accepted but never required.
- Creation happens only after an explicit local preview and confirmation.
- `@everyone` is denied `View Channel`; the configured member and the bot each
  receive only the permissions a chat needs, and never `Administrator` or
  `Manage Channels` in-channel.
- Reuse requires an exact match on guild, intended user, and permission
  overwrites. A name collision, permission drift, or wrong-user channel stops the
  run instead of hijacking it.
- Every created channel is read back; differing privacy or membership fails
  rather than reporting success.
- Channel IDs reach only owner-only private runtime configuration.
- Reruns are idempotent and create nothing.
- Synthetic REST fixtures live in `fixtures/discord.provisioning.json`, with
  negative tests for wrong guild, wrong bot, missing `Manage Channels`, name
  collision, partial creation, permission drift, forbidden, timeout, ambiguous
  response, and unavailable readback.
- An unconfirmable create is recorded as unknown; a later run refuses to create
  another and asks for reconciliation first.

### 2. Full route profile, bounded client profiles — done, with one open item

`assistant/scotty_business/routing.py`, enforced from
`ingress.py` (`pre_gateway_dispatch`) and `runtime.py`
(`resolve_enabled_toolsets_for_source`).

- Client guild/channel sources resolve to per-role bounded profiles carrying
  only the `scotty` toolset.
- The exact route guild, private channel, and user resolve to a separate full
  profile. The exact user ID is checked here because native profile routing
  matches guild and channel but not the acting user.
- A wrong user in the route channel is rejected before session or model
  activity, silently, with no reply at all.
- Toolset resolution fails closed. An unresolved source, or unavailable private
  configuration, yields no model toolset — never a wider one.
- Configuration rejects a route that shares a client guild, channel, or
  principal tuple, so the route can never collapse into a client surface.
- Client-visible Discord destinations are built from the client principals and
  announcement channels only, so no client-visible tool can reach the route
  channel and no fixed or proactive delivery path points at it.
- Client profile state stays profile-local: each role has its own profile name,
  and shared business data is reached only through bounded provider records.
- `tests/test_maintainer_secrecy.py` proves no fixed client-facing string, tool
  schema, prompt section, or profile name carries a route identifier or
  discloses that a hidden route exists.

**Open item.** The pinned runtime's own contracts
(`gateway/profile_routing.py`, `gateway/run.py::_resolve_enabled_toolsets_for_source`,
`gateway/platforms/base.py::toolsets_for_source`) could not be inspected while
preparing this change, because the pinned image is not available in the
environment used. Rather than assume a YAML schema, the generated `config.yaml`
uses only keys already proven at `cbc8c83`, and the native profile-routing block
is written beside it as `scotty/profile-routing.overlay.yaml`, owner-only and
explicitly not merged. Verify those three contracts against the pinned image,
then merge the overlay and confirm the hook name
`resolve_enabled_toolsets_for_source` matches the runtime's own hook. That hook
is registered defensively and is not declared in `plugin.yaml`, so a runtime
that does not offer it still loads the plugin unchanged. Until the name is
confirmed, the failure direction is safe: the route degrades to the bounded
client toolset rather than widening any client surface, and authorization still
runs in `pre_gateway_dispatch`.

### 3. Provider setup guidance — done

`assistant/scotty_business/guidance.py`, surfaced through
`scotty_read` with `operation: provider_setup`. No new tool is registered and the
model inventory is unchanged at five tools.

- Discord, Trello, GoHighLevel, RentCast, and a guidance-only Google Workspace
  entry each state `not connected` when unconfigured, name the identifiers and
  scopes to gather, list the provider-side steps, and direct the operator to the
  local setup command.
- No guidance string asks for a credential in Discord or accepts one from chat.
- Discord guidance asks for `Manage Channels`, never `Administrator`.
- Google Workspace is documented only. It is not installed, holds no add-on
  slot, and adds no adapter.
- A missing Trello, GoHighLevel, or RentCast credential no longer prevents the
  runtime from loading. That provider degrades to a stand-in that refuses every
  call before any network request.

### 4. Final setup and acceptance handoff — done, with one open item

See `docs/scotty-basic-release-commands.md` for the final install/configure
command, the Codex OAuth step, `make acceptance`, the route, main-operator, and
employee acceptance prompts, the exact wizard command, and the `not connected`
behaviour.

**Open item.** The exact Codex OAuth subcommand for pinned Hermes `0.20.6` is
not recorded here. `make oauth-probe` reads it from the image itself in a
disposable, network-disabled container; run it on the deployment host and record
the printed command. Guessing a command name would have been a runtime
assumption, which this handoff forbids.

### Preserved

The six-add-on cap and its fixed response, the approval state machine and its
bound approver, tuple-scoped reminders, adapter isolation, the fixed coding
refusal, credential redaction, and the `cbc8c83` behaviour are unchanged. The
model inventory is still exactly the five bounded Scotty tools.
