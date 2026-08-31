# Scotty Google Workspace WIP continuation contract

## Authority, repository, and checkpoint provenance

This is the controlling handoff for a fresh Claude App session on this branch.

- Repository: `MarcoFernstaedt/scotty-deployment`
- Canonical remote: `https://github.com/MarcoFernstaedt/scotty-deployment.git`
- Required working branch: `feature/scotty-google-one-command`
- Accepted base: `origin/main` at `c5bbb65169cb1f4dd8fbf49ffaf9b0f80f4afc8e`
- Base result: PR #1, `feature/scotty-basic-assistant`, was squash-merged as that commit.
- Pre-checkpoint source HEAD: `c5bbb65169cb1f4dd8fbf49ffaf9b0f80f4afc8e`
- WIP checkpoint identity: the branch HEAD containing this file. Verify it with `git rev-parse HEAD`, confirm it descends from the accepted base, and confirm the branch and remote before editing.

Do not work on `main`. Do not rebase, reset, force-push, rewrite history, or transplant work from `origin/claude/file-instructions-ynzqya`. That older Claude branch is stale and differs from merged main in `SHA256SUMS`, `tests/test_pinned_smoke_contract.py`, and `tools/pinned_smoke.py`.

This branch is an intentional interrupted WIP checkpoint. It is not accepted, passing, deployable, releasable, or safe to activate. The implementation changes were preserved for continuation; their presence is not evidence that their behavior is correct.

## Precedence and required reading

Read, in order, before editing:

1. this root `CLAUDE.md`;
2. `docs/scotty-basic-release-engineering-contract.md`;
3. `README.md`;
4. `docs/scotty-basic-release-commands.md` and `docs/scotty-basic-operations.md`;
5. `Makefile`, `install.sh`, `scotty-start`, `compose.yaml`, `setup-scotty`;
6. all plugin, guard, fixture, tool, and test files affected below;
7. the pinned Hermes Agent 0.20.6 runtime contracts named under “Pinned runtime evidence.”

`docs/claude-app-handoff.md` is historical. Keep it unchanged. This root file supersedes its branch, baseline, remaining-work, and Google-guidance-only instructions for `feature/scotty-google-one-command`. Retain the accepted release-one boundaries and completed main behavior documented there unless this contract explicitly broadens Google Workspace or Scotty-owned repair.

When documents and implementation disagree, do not guess. Preserve the narrower accepted security boundary, write a failing synthetic test for the intended corrected behavior, and resolve the contradiction explicitly.

## First action: audit the entire partial implementation

Before changing any byte:

```sh
git fetch origin --prune
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git merge-base origin/main HEAD
git status --short --untracked-files=all
git diff origin/main...HEAD --
git worktree list --porcelain
```

Prove that the canonical repository and branch match this file and that no other writer owns the worktree, branch, or index. Audit every partial implementation path below; do not assume any is complete, internally consistent, packaged, authorized correctly, or tested.

Modified checkpoint paths:

- `Makefile`
- `assistant/scotty_business/__init__.py`
- `assistant/scotty_business/adapters/__init__.py`
- `assistant/scotty_business/adapters/http.py`
- `assistant/scotty_business/config.py`
- `assistant/scotty_business/guidance.py`
- `assistant/scotty_business/policy.py`
- `assistant/scotty_business/runtime.py`
- `assistant/scotty_business/service.py`
- `assistant/scotty_business/setup.py`
- `assistant/scotty_guard/guard.py`
- `fixtures/scotty.private.discord-only.example.json`
- `fixtures/scotty.private.example.json`
- `install.sh`
- `tests/synthetic.py`
- `tests/test.sh`
- `tests/test_fixtures.py`
- `tests/test_guidance.py`
- `tests/test_installer_package.py`
- `tests/test_policy_and_calculations.py`
- `tests/test_provider_connection.py`
- `tests/test_setup.py`
- `tools/synthetic_acceptance.py`

New checkpoint paths:

- `assistant/scotty_business/adapters/google_workspace.py`
- `assistant/scotty_business/google_oauth.py`
- `assistant/scotty_business/google_policy.py`
- `scotty-start`
- `tests/test_google_workspace.py`
- `tests/test_onboarding_and_start.py`
- `tests/test_self_repair.py`
- `CLAUDE.md`

The self-repair test imports `assistant.scotty_business.self_repair`, but that module does not exist at this checkpoint. Do not mask or delete the test; implement the bounded behavior test-first when you continue the mission.

## Corrected objective

Finish a generic, public-repository-safe release that gives Trent practical day-to-day control of his own explicitly authorized Google Workspace account across:

- Gmail: search, read, organize, label, archive, draft, reply, and approval-bound exact sends;
- Calendar: search/read, create, update, reschedule, and cancel, with new external audiences consequence-gated;
- Drive: find, create, read, update, move, and trash, with permanent deletion and sharing/permission changes separately gated;
- Docs: find through Drive, create, read, and update;
- Sheets: find through Drive, create, read, and update values and structure;
- Contacts: list/search, create, read, and update, with permanent deletion consequence-gated.

Do not reduce this to read-only access or a static allowlist of a few document, file, label, calendar, spreadsheet, or contact IDs. Broad OAuth consent enables product access; it does not grant broad autonomous authority.

Ordinary reversible work in Trent’s authorized Workspace must not repeatedly require approval. Deterministic consequence gates are required for:

- exact email sends and any newly introduced external audience;
- permanent deletion;
- public sharing and permission changes;
- Google Admin, account-security, and billing actions;
- bulk or otherwise high-impact mutation;
- credential creation, rotation, revocation, or disclosure.

Unknown operations fail closed. Approval must bind the exact actor tuple, action class, target, canonical payload hash, source revision, expiry, version, and execution nonce. An approval for one action, audience, payload, resource, or actor must never authorize another.

## Scotty-owned diagnosis and repair

Add governed health inspection and narrow repair only for Scotty’s own client-owned integrations, declarative workflows, schedules, caches, and ordinary configuration. Prefer native Hermes and declarative configuration contracts over ad hoc control paths.

Allowed repair may restore or reconcile bounded Scotty-owned state when tests prove containment, authorization, idempotency, crash consistency, and redacted receipts.

Never expose or implement through a client tool:

- secret or credential reading;
- arbitrary root, shell, process, code execution, filesystem access, or package installation;
- Docker, systemd, firewall, host governance, or service administration;
- Imperator, Vaultwarden, maintainer-private systems, or cross-client state;
- arbitrary cron or schedules outside Scotty’s narrow declarative ownership;
- plugin, skill, MCP, GitHub, browser, Tool Search, or delegation management.

If a genuine repair requires privilege, stop at a narrow redacted diagnosis and require an explicitly designed root-owned operator recovery command. Never let the model synthesize privileged commands.

## Onboarding, secrets, and start path

The fixed private Trent onboarding flow must actively guide Trent through complete setup of Discord, Trello, Google Workspace, GoHighLevel, RentCast, and later approved integrations. It must explain what each provider enables, link or describe the current provider-side developer-console steps, identify required APIs/scopes/account or resource IDs, validate non-secret identifiers, show setup status, diagnose setup failures, and resume at the first unfinished step. Google must use provider-owned OAuth browser consent where applicable. The flow must be usable conversationally from Trent's private authorized channel instead of merely referring him to operator documentation.

Trent must be able to hand Scotty API keys and comparable credentials through a purpose-built protected credential-intake flow initiated from his exact authorized private tuple. Do not interpret this as permission to accept a normal Discord message containing a secret. The intake must intercept the next expected credential before event construction, batching, persistence, queues, sessions, model dispatch, tools, logs, or ordinary chat history; bind it to the exact authenticated bot/profile/user/guild/channel, expiry, and expected provider/credential class; validate through a fixed privilege-separated broker; delete the exact source message; and commit only after the platform confirms deletion. Delete, validation, provenance, timeout, replay, conflict, or commit failure must consume/abort without persistence. The broker must never return or log the secret, and Scotty may report only fixed redacted status such as credential present, validation passed/failed, and next setup step. If Discord cannot provide confirmed source deletion or the physical privilege boundary is not installed and verified, fail closed and direct Trent to the approved hidden local or secure operator entry path instead.

The onboarding experience must include credential replacement and repair guidance without revealing stored values. It may open a new one-time intake window for the exact credential class, revalidate the provider, update connection status, and preserve the old credential until the new one is confirmed working when the provider permits safe rollback. Credential creation, rotation, revocation, account-security changes, or destructive provider actions remain consequence-gated and must never be inferred from a generic request to "fix it."

Secrets and OAuth material may enter only through the verified pre-dispatch protected intake above, local hidden input, an approved protected local file, process environment where already accepted, or provider-owned browser consent. Never allow credential material to reach ordinary chat history or model context, and never put credentials, OAuth client secrets, authorization codes, access/refresh tokens, cookies, or private IDs in argv, Git, fixtures, logs, stdout, test evidence, exception text, or model context. Redact `repr`, status, errors, and receipts. Use synthetic fixtures only. A credential already posted through an ordinary model-visible path is exposed and must be rotated; do not silently reuse it.

The intended installed root-only lifecycle command is:

```sh
sudo /usr/local/sbin/scotty-start
```

Audit and finish that command before treating it as safe. It must validate the prepared stopped container, run owner-only setup, invoke the pinned runtime’s supported Codex OAuth path, complete/validate Google provider consent when selected, start only the prepared Scotty container, run non-sending health checks, and leave Scotty stopped with one concise recovery instruction on incomplete prerequisites or partial failure. It must not send onboarding automatically. Do not add a competing start interface unless a test-backed safety requirement makes a thin clean-checkout orchestrator necessary.

## Invariants that must remain true

Preserve all accepted main behavior except the explicit Google and Scotty-owned-repair expansion:

- exact pre-model Discord `(guild_id, channel_id, user_id, role)` authorization;
- denial of wrong guild, channel, user, thread parent, bot author, and every mixed cross-product;
- separate full maintainer profile and bounded Trent/employee profiles with profile-local sessions, homes, memories, configuration, and toolsets;
- no maintainer route identifiers or hidden-route disclosure in client-visible strings, state, prompts, errors, tools, or logs;
- exactly the bounded Scotty model tool inventory; do not add generic tools to obtain Google or repair capability;
- employee may propose but cannot approve or execute consequence-bearing work;
- immutable approval state transitions, `BEGIN IMMEDIATE`, compare-and-set claims, exact actor/action semantics, nonce single use, readback, and reconciliation;
- timeout, malformed acknowledgement, failed readback, crash during execution, or otherwise ambiguous external effect becomes `unknown` and is never blindly retried;
- idempotent setup, recovery, provider mutation, and schedule/workflow operations with explicit crash-lineage tests;
- no public ports, Docker socket, privileged mode, extra mounts, private/Tailnet destination access, or automatic ambiguous-write retries;
- no automatic onboarding message; only the exact authorized maintainer trigger may deliver the fixed wizard after activation;
- no live external mutations during development or test.

Use RED/GREEN development. First reproduce each current failure and add or strengthen a focused synthetic regression; then make the smallest implementation correction; then rerun focused and governing gates. Do not weaken assertions, delete failing tests, replace behavior with mocks-only evidence, or update checksums to hide an incomplete package.

## Known checkpoint failures and incomplete behavior

Evidence was collected on the interrupted working tree before this WIP checkpoint. It is discovery evidence, not acceptance.

Passing discovery:

- `git diff --check` exited 0.
- Python compilation of `assistant`, `tests`, `tools`, and `setup-scotty`, with bytecode redirected outside the repository, passed.
- `shellcheck -x install.sh scotty-start firewall/scotty-egress-guard tests/*.sh` exited 0.
- `python3 tools/scan_repository.py` passed: 84 working files and 180 reachable historical blobs, with no scanner-detected secret patterns.

Failing or unavailable discovery:

- Direct unit discovery ran 287 tests and ended `FAILED (failures=2, errors=8)`.
- `tests/test_self_repair.py` cannot import the absent `assistant.scotty_business.self_repair` module.
- Three fixture/acceptance errors arise because `fixtures/scotty.private.example.json` still has an obsolete resource-allowlist Google shape while `RuntimeConfig` now requires exactly `account_email` plus `oauth_scopes`.
- Four existing setup tests raise `StopIteration` because the partial setup added a new visible Google prompt without reconciling existing input contracts.
- Two installer package tests fail because `google_policy.py` is absent from `install.sh`’s staged plugin inventory.
- `python3 tools/generate_checksums.py --check` fails with `SHA256SUMS is stale or incomplete`.
- `sha256sum -c SHA256SUMS` reports 23 checksum mismatches.
- `make verify` stops at `make format-check` because `uvx` is unavailable in the checkpoint host PATH; formatting, Ruff lint, mypy, package, pinned smoke, and later `make verify` phases therefore were not accepted.
- `make smoke` and `make oauth-probe` were not run for this checkpoint. No pinned-runtime, Docker, provider, credential, install, start, or live acceptance claim exists.

Additional partial areas requiring audit include the correctness and completeness of Google REST payloads/readback, account binding and token refresh lifecycle, exact send/new-audience classification, bulk thresholds, proposal integration, client tool schemas, OAuth loopback failure closure, setup prefill sequencing, start-command stop/recovery semantics, synthetic acceptance, package contents, checksums, docs, and the full Scotty-owned repair design.

## Governing commands

Discover prerequisites first; do not install global tooling or change product code merely to satisfy the host. Run repository-native commands from the root and preserve exact output and exit status.

```sh
git diff --check
make format-check
make lint
make typecheck
make test
make acceptance
make package
make smoke
make scan
make checksums
make verify
make oauth-probe
```

The underlying governing commands include:

```sh
uvx ruff@0.12.9 format --check assistant tests tools setup-scotty
uvx ruff@0.12.9 check assistant tests tools setup-scotty
shellcheck -x install.sh scotty-start firewall/scotty-egress-guard tests/*.sh
uvx mypy@1.17.1 assistant/scotty_business assistant/scotty_guard tools
./tests/test.sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/synthetic_acceptance.py
python3 tools/build_package.py
cd dist && sha256sum -c scotty-business-1.0.0.tar.gz.sha256
cd dist && sha256sum -c scotty-guard-1.0.0.tar.gz.sha256
python3 tools/pinned_smoke.py
python3 tools/pinned_oauth_probe.py
python3 tools/scan_repository.py
python3 tools/generate_checksums.py --check
sha256sum -c SHA256SUMS
git status --short --untracked-files=all
```

After any checksum or deterministic package generation, inspect the exact diff and prove regeneration is deterministic. Run current-tree and full reachable-history scans. Before commit, inspect staged bytes, file modes, generated/ignored artifacts, dependencies, `.env`/DB/cache/log exclusions, and every new file.

## Pinned runtime evidence

Hermes Agent 0.20.6 is pinned by immutable image digest in this repository. Inspect the actual pinned runtime rather than guessing its API, CLI, configuration, profile, hook, OAuth, or tool contracts. At minimum re-verify:

- `gateway/profile_routing.py`;
- `gateway/run.py::_resolve_enabled_toolsets_for_source`;
- `gateway/platforms/base.py::toolsets_for_source`;
- Discord `SessionSource` and thread-parent behavior;
- gateway sender authorization and `pre_gateway_dispatch` hook behavior;
- profile-home/config loading and per-profile model/tool resolution;
- the exact `hermes auth add openai-codex` command and its container interaction requirements.

`make smoke` must exercise the exact staged package/config against the pinned image, credential-free and network-disabled. A host-side imitation is not pinned-runtime acceptance.

## Public/private and external-effect boundary

This is a public generic repository. Only generic code, schemas, docs, and synthetic fixtures belong in Git. Never commit or expose real names in configuration, user/guild/channel IDs, provider account/location/board IDs, private guild/channel names, client records, credentials, OAuth material, runtime databases, sessions, logs, caches, private topology, or commercial terms. Synthetic Discord snowflakes and `example.invalid` identities are permitted test data only.

During implementation and verification:

- do not call live Discord, Google, Trello, GoHighLevel, or RentCast APIs;
- do not send messages, email, invitations, events, or provider writes;
- do not touch `/srv/Scotty`, Vaultwarden, Imperator, VPS services, live containers, firewall, systemd, credentials, or client state;
- use disposable credential-free fixtures and environments only;
- do not deploy, activate, restart services, release, or merge.

Stop immediately on any real secret/private identifier, uncertain external effect, cross-client path, runtime contract incompatibility, need for a forbidden tool, or requirement to weaken authorization/isolation.

## Completion, review, Git, and stopping rules

Continue until all corrected requirements have focused RED/GREEN evidence and every applicable governing gate passes on the exact candidate. Then:

1. review tracked, staged, untracked, ignored/generated, package, checksum, mode, dependency, secret, private-ID, and reachable-history state;
2. freeze the exact candidate and obtain a fresh independent read-only review;
3. fix material findings and rerun affected plus governing gates;
4. commit focused implementation units on `feature/scotty-google-one-command` only;
5. fetch and push without force;
6. verify local HEAD equals `origin/feature/scotty-google-one-command` and remote blobs match;
7. open or update one PR to `main` only after the implementation and all gates pass;
8. do not merge, deploy, activate, use live credentials, or perform live acceptance.

Stop with a precise blocker instead of claiming completion if any test, formatter, lint, typecheck, package, checksum, scan, pinned smoke, OAuth probe, review, provenance, authorization, idempotency, crash-consistency, or privacy gate is failing, unavailable, ambiguous, or stale.

## Claude App starting prompt

> Open `MarcoFernstaedt/scotty-deployment` on branch `feature/scotty-google-one-command`, read and obey root `CLAUDE.md`, audit every preserved WIP file before editing, reproduce the documented failures, and finish full Google Workspace, guided Discord/Trello/Google/GoHighLevel/RentCast setup, pre-dispatch protected API-key intake, one-command start, and bounded Scotty-owned repair with RED/GREEN synthetic tests and all governing gates; never use live credentials or external mutations during development, never work on main, and stop rather than weakening the accepted security boundaries.
