---
paths:
  - "assistant/scotty_business/{self_repair,credential_intake,approvals,reminders,runtime,service,setup}.py"
  - "assistant/scotty_guard/**/*.py"
  - "install.sh"
  - "scotty-start"
  - "setup-scotty"
  - "compose.yaml"
  - "firewall/**/*"
  - "tests/{test_self_repair,test_credential_intake,test_onboarding_and_start,test_installer_package,test_pinned_smoke_contract}.py"
  - "tools/**/*"
---

# Reliability, credential boundary, and release rules

Do not promise “never breaks.” Engineer measurable availability, automatic recovery, safe degradation, deterministic reconciliation, tested rollback, and fast operator escalation.

## Independent supervisor

Reliability cannot depend only on the model. Deterministically supervise singleton Discord consumption, service/process state, controlled restart, bounded backoff/crash-loop detection, gateway/Discord health, redacted provider auth/connectivity, databases/locks/leases, disk, paths, ownership/modes, sockets, manifests, and schedules. Alert Marco once per material incident/recovery.

## Allowed automatic repair

Only allowlisted product-owned state: stale locks and recoverable leases, derived caches/indexes, expected product-directory permissions, temporary files, known process restart conditions, deterministic workflow reconciliation, rebuilding derived state, and rollback to the last independently accepted immutable release after bounded recovery fails.

Never self-modify enforcement policy, startup gates, approvals, identities/roles, tools, plugins, skills, MCP, models/providers, credentials/OAuth mappings, firewall, systemd, Docker, packages, users/groups, sudoers, SSH, secrets, release signatures, rollback controls, or audit history. Never access Imperator/another client, rotate credentials, or self-approve. Unsafe/exhausted repair returns a redacted diagnosis and fixed management proposal.

Provider failure degrades only the affected capability. Use `healthy`, `degraded`, `blocked`, `unknown`, and `not configured`; never call unknown healthy. Prevent catch-up storms and duplicate effects.

## Credential intake

The reviewed baseline lacked a packaged broker and intercepted after event construction. Implement and package a fixed-operation privilege-separated broker only if the pinned Discord runtime can prove interception/deletion before event construction, persistence, queues, sessions, logs, model dispatch, and tools. Otherwise disable Discord secret intake and retain hidden local/operator intake. Never fake or weaken the physical boundary.

Test real installed socket/service/store/mount lifecycle, permissions, unauthorized callers, wrong actor/channel, expiry/replay, malformed/oversized frames, symlinks/path substitution, broker restart/unavailability, replacement/revoke behavior, and redaction.

## Durable effects

Every external mutation needs durable intent/effect tracking bound to actor/profile, provider/account, operation/schema, target revision, immutable payload/preview hash, approval, idempotency key, attempt lineage, acknowledgement, authoritative readback, and verified/failed/unknown state. Never blindly retry ambiguous effects. Cross-system workflows use explicit saga/compensation semantics.

## Start, release, backup, and rollback

- Reconcile every failed Docker start with authoritative exact managed-container state; stop an incompletely accepted running container.
- Version and hash-bind releases, policy, config, schemas, migrations, and manifests.
- Test backup/restore and rollback of non-secret state, schedules, workflows, memory classification, and provider mappings.
- Rollback must not start a second Discord consumer, replay schedules, lose reconciliation records, or duplicate effects.
- Add configurable budgets, rates/concurrency, quiet hours, workflow caps, batch thresholds, retention, circuit breakers, and escalation rules.
- Prepare—but do not execute—migration to Marco’s future upgraded server plan.

No live credentials, providers, deployment, activation, external sends, merge, or repository rename during implementation/testing.