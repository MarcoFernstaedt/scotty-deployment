# Scotty Basic Operations and Acceptance

This is an operator procedure, not authorization to activate or access live providers. Installation and private setup leave the container stopped. A separate approved operations window is required for every step below.

## Pre-activation evidence

1. Verify the exact repository commit, clean branch, and `SHA256SUMS`.
2. Run `make verify` without credentials.
3. Run `sudo ./install.sh` only on the intended clean host. Confirm the final message states the container was created and never started.
4. Run `sudo /srv/Scotty/operator/setup-scotty` locally. Never pass credentials on argv, paste them into Discord, or save them in shell history.
5. Confirm private files below `/srv/Scotty/data` are UID/GID 10000 and mode `0600`; directories are mode `0700`.
6. Confirm the container is still stopped, no ports are published, one bind mount targets `/opt/data`, and the pinned image digest is unchanged.
7. Confirm the Discord application has Message Content Intent enabled, only the required bot permissions, membership in the configured guild, and access only to configured private channels.
8. Confirm Trello credentials are scoped operationally to the configured board resources, the GoHighLevel Private Integration belongs to one internal location with the least scopes needed, and RentCast is read-only.

## Staged live acceptance

During an approved activation window:

1. Start only the prepared `scotty` container using the approved operator procedure. Do not add ports, restart policy, sockets, mounts, or tools.
2. Verify the gateway loads the `scotty-business` plugin and the model-facing inventory is exactly:
   - `scotty_read`
   - `scotty_propose`
   - `scotty_approval`
   - `scotty_reminder`
   - `scotty_calculate`
3. Verify unauthorized Discord messages are silently ignored for wrong guild, channel, user, mixed tuples, and wrong-parent threads.
4. From each configured private channel, verify a distinct session ID and no transcript or memory content crosses channels.
5. Perform read-only checks first:
   - one configured Trello board/card read;
   - one configured GoHighLevel contact/conversation read;
   - one RentCast property, value estimate, rent estimate, and comparable response.
6. Verify returned source IDs, location/board bindings, revisions, retrieval times, missing fields, and preliminary-analysis disclaimer.
7. Verify the maintainer's exact fixed command sends the fixed wizard only to the main-operator channel. Nothing is sent automatically.
8. Verify an authorized fixed employee-summary request sends only the fixed summary to the employee channel.
9. Verify one private reminder per role is delivered only to that role's bound channel and survives a clean restart without duplicate delivery.
10. Perform one harmless bounded acceptance action for each write-capable provider only after reviewing and approving the exact proposal:
    - create/update/move/archive a disposable synthetic Trello card, including destination readback before duplicate archive;
    - send one pre-agreed SMS to a designated acceptance contact through GoHighLevel, then verify it from the authoritative conversation source;
    - send one pre-agreed Discord announcement to a configured acceptance channel, then read it back.
11. Do not repeat a timeout, malformed acknowledgement, or failed readback. Confirm the proposal becomes `unknown` and reconcile from provider truth.

## Stop conditions

Stop immediately if any forbidden tool appears, Tool Search replaces the five Scotty tools, a tuple mismatch reaches the model, a slash command changes runtime state, a provider response crosses configured scope, an ambiguous write is retried, a credential appears in logs/chat, the image/runtime contract drifts, a public webhook/port is required, or any fifth add-on/future application feature is requested.

## Rollback and compensation

Before activation, keep the container stopped and use the installer's in-process cleanup only if installation itself fails. There is intentionally no persistent uninstall artifact.

After activation, stop the Scotty container through the separately approved operator path. Preserve `/srv/Scotty/data/scotty/*.db` and provider receipts for reconciliation; do not delete unknown or executing records. Revoke or rotate provider credentials if exposure is suspected. Compensate only through provider-native, reviewed actions: archive a disposable Trello card, document SMS status rather than attempting recall, and remove a test Discord announcement only with explicit approval. Host firewall, systemd, data deletion, and full uninstall require an exact separate change plan.
