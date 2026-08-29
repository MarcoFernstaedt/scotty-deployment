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

Everything in "Remaining objective" above is implemented on this branch. Two
independent reviews of `8ef7442` found native-integration defects; this section
records the corrected state.

### 1. Private Discord channel provisioning — done

`assistant/scotty_business/provisioning.py`, wired into `setup.py`.

- The bot token is read from hidden local input or an exported environment
  variable; `setup.py` contains no `argv` or `argparse` use.
- Bot identity, guild identity, guild membership, and `Manage Channels` are
  verified before any mutation. `Administrator` is accepted but never required.
- Creation happens only after an explicit local preview and confirmation, denies
  `View Channel` to `@everyone`, and grants the configured member and the bot
  only the permissions a chat needs.
- Reuse requires an exact match on guild, intended user, and permission
  overwrites. A name collision, permission drift, or wrong-user channel stops the
  run instead of hijacking it.
- Every created channel is read back; differing privacy or membership fails.
- An unconfirmable create is recorded as unknown and never resolved by creating a
  second channel, on that run or a later one.

### 2. Native multiplexed profile routing — corrected

The previous top-level `profiles:`/`routing:` overlay was not a native contract
and created zero routes. It is gone. Setup now renders the runtime's own keys:

```yaml
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist: [...]
  profile_routes:
    - name: ...
      platform: discord
      guild_id: ...
      chat_id: ...
      profile: ...
```

- Exactly three routes are rendered, using only `name`, `platform`, `guild_id`,
  `chat_id`, and `profile`. `channel_id` never appears in a route.
- `routing.parse_profile_routes` and `routing.match_profile_route` model that
  contract and validate the generated configuration before a host loads it.
  `render_hermes_config` parses its own output and refuses to emit a
  configuration that does not satisfy it.
- `make smoke` loads the generated configuration inside the pinned image with the
  runtime's own YAML loader and asserts three routes, the native key set, the
  served allowlist, and each profile's toolset.

### 3. Three real, separately served profiles — corrected

- `scotty-maintainer` is a normal full profile: no bounded plugin, the normal
  tool inventory, and no Scotty client identity section.
- `scotty-main-operator` and `scotty-employee` load `scotty-business` and expose
  only the `scotty` toolset.
- The installer creates one home per served profile under
  `/srv/Scotty/data/profiles/` and stages the bounded plugin into the two client
  homes only. It verifies the full profile home carries no plugin.
- `ensure_profile_homes` creates or idempotently verifies each home and its
  configuration, and fails closed when a routed profile has no home, a client
  home lacks its staged plugin, or the full profile home carries one. There is no
  silent fall back to the default profile.
- The base configuration is bounded, so a profile whose override fails to apply
  is bounded rather than unbounded.

### 3b. Gateway sender authorization — added

The generated `.env` carries `DISCORD_ALLOWED_USERS`, built deterministically
from `route_user_id`, `operator_user_id` and `employee_user_id`. No wildcard, no
role authorization, no `DISCORD_ALLOW_ALL_USERS`, and no manual post-install
pairing. That is the gateway admission layer only; exact guild x channel x user
tuple enforcement still runs before model dispatch and rejects every mixed
tuple, including a mixed tuple whose sender is on the allowlist.

### 3c. Fixed Trent wizard — restored

`Scotty, send Trent the setup wizard.` works again. Only the exact maintainer
tuple triggers it, it is handled before model execution, the destination comes
from private configuration rather than the model, and one inbound message
delivers exactly once even when two hooks observe it. Wrong users and mixed
tuples get no wizard, no reply and no disclosure. Nothing is sent automatically
after installation, and the wizard text never asks for a credential. The fixed
employee summary is unchanged.

### 4. The inert plugin toolset hook — removed

`resolve_enabled_toolsets_for_source` is not a plugin lifecycle hook and was
never invoked. Its registration, its `Controller.toolsets_for_source`
implementation, and its manifest entry are all removed. `pre_gateway_dispatch`
is the only hook registered, and a regression test asserts exactly that. Tool
boundaries now come from native profile isolation alone.

### 5. Codex OAuth-only initial setup — done

- Provider `openai-codex` is supported and collects no model API key.
- Setup prints the native command `hermes auth add openai-codex` and instructs
  the maintainer to complete the flow locally.
- No OAuth token is collected, stored, printed, logged, or written to `.env`,
  argv, tests, fixtures, or this repository. `make oauth-probe` only confirms the
  subcommand exists in the pinned image.

### 6. Deferred optional provider credentials — done

- Initial setup requires only the Discord bot token, the Discord identifiers, and
  the local Codex OAuth step.
- Trello, GoHighLevel, RentCast, and Google Workspace are optional. Their
  configuration sections are omitted entirely when unconfigured; no placeholder
  is ever recorded as a connection.
- A provider counts as connected only when both its credential and its configured
  resource scope are present. Otherwise Scotty starts normally and reports
  `not connected` with fixed setup guidance.
- `fixtures/scotty.private.discord-only.example.json` is the synthetic
  Discord-only starting state.

### 7. Private route validation — done

`setup.validate_maintainer_route` reads the route back from Discord before any
configuration is written, and rejects a route that does not exist, is public, is
in the client guild, is in a different guild than configured, is not a text
channel, cannot be viewed by the configured user, cannot be viewed, posted to and
read by the assistant, or that either client principal can view or whose guild
either client principal belongs to. Every failure message is generic and names no
private identifier.

### 8. Client authorization and channel privacy — preserved

The exact Discord tuple is still enforced before model dispatch by the plugin's
`pre_gateway_dispatch` gate inside the client profiles, binding the acting user
that native routing does not match. Wrong guild, channel, user, thread parent,
bot author, and every mixed cross-product receive no model run and no reply.
Private-channel provisioning remains idempotent with permission readback, and
`Administrator` is never required.

### Pre-model enforcement paths

There are two, and each is demonstrable rather than assumed:

1. **Client profiles.** `scotty-business` is staged and enabled in each client
   profile home and at the gateway root. Its `pre_gateway_dispatch` hook binds
   the exact guild, channel and user, and rejects every mixed tuple before any
   model runs.
2. **The full maintainer profile.** `scotty-guard` is staged and enabled only in
   that profile home. It registers one `pre_gateway_dispatch` hook and nothing
   else: no model tools, no prompt section, no bounded client identity. It
   supplies the user match that native profile routing does not perform, and it
   owns the fixed wizard dispatch.

The guard exists precisely so that maintainer enforcement does not depend on
whether a root-registered hook also runs for a multiplexed profile turn.
`make smoke` drives both paths inside the pinned image over the full
admit/deny matrix, and `tests/test_pinned_smoke_contract.py` drives the same
staged layout on the host.

### Remaining pinned-runtime uncertainty

`make smoke` is the only place these can be settled, and it needs the pinned
image:

- Whether the runtime reads `profiles/<profile>/config.yaml` from each profile
  home. The staging is hedged: the bounded plugin is present in each client home
  and absent from the full profile home, and the full profile's own
  configuration enables only the guard. Under either reading the failure
  direction is bounded clients, never an unbounded one.
- The exact signature of `gateway/authz_mixin.py::_is_user_authorized`. The
  probe locates it by name and drives it by introspection; if it cannot, the
  smoke fails and prints the real signature rather than passing silently.
- Whether the root-registered `pre_gateway_dispatch` hook runs for multiplexed
  maintainer turns. The probe records which hook source it obtained. Enforcement
  does not depend on the answer, because the profile-local guard covers it.
