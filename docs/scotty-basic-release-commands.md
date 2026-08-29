# Scotty Basic Release: Final Commands and Acceptance Prompts

Every command below is run by the maintainer on the deployment host or in a
checkout. No command in this file accepts a credential on the command line, and
no prompt in this file asks anyone to send a credential through Discord.

Placeholders such as `<guild id>` are never filled in inside this repository.
Real identifiers live only in owner-only private runtime configuration below
`/srv/Scotty/data`.

## 1. Verify the candidate before touching a host

From a clean checkout, with no credentials present:

```sh
make verify
git diff --check
git status --short --untracked-files=all
```

`make verify` runs format, lint, ShellCheck, mypy, the unit tests, the
credential-free synthetic acceptance run, the deterministic package build and
checksum, the pinned-image plugin smoke, the repository and history secret scan,
and the checksum inventory.

## 2. Final install and configure command

Install without activation, then configure privately. These are the only two
commands needed on the host:

```sh
sudo ./install.sh && sudo /srv/Scotty/operator/setup-scotty
```

`install.sh` stages the plugin, creates the root-owned operator files, activates
the host egress guard, creates the bridge, and creates the container. It never
starts the container.

`setup-scotty` then runs as root while the container is stopped and:

1. verifies the `scotty` container exists and is stopped;
2. reads every credential with hidden terminal input, or from an exported
   environment variable, never from `argv`;
3. offers to create the main-operator and employee private channels, or accepts
   existing channel IDs;
4. previews each channel it would create and waits for an explicit `yes`;
5. reads each created channel back and stops if privacy or membership differs;
6. optionally records the private full-profile route;
7. verifies the bot identity, guild membership, and channel privacy;
8. writes `config.yaml`, `.env`, `scotty/private.json`, and (when a route is
   configured) `scotty/profile-routing.overlay.yaml` atomically with mode `0600`
   and UID/GID 10000;
9. leaves the container stopped.

To supply the bot token from the environment instead of a hidden prompt:

```sh
sudo --preserve-env=DISCORD_BOT_TOKEN /srv/Scotty/operator/setup-scotty
```

Export it in a shell configured to keep it out of history, and unset it
afterwards.

### Channel provisioning behaviour

- Reruns are idempotent. An existing channel is reused only when its guild,
  intended user, and permission overwrites match exactly.
- A name collision, a permission drift, or a channel belonging to another user
  stops the run instead of hijacking the channel.
- A create whose outcome cannot be confirmed is recorded as unknown. Setup
  refuses to continue and never creates a second channel; reconcile the channel
  in Discord first.
- The application needs `Manage Channels` in the guild plus normal messaging
  permissions. Discord `Administrator` is never required.

## 3. Codex OAuth step

The OAuth subcommand must be the one the pinned Hermes `0.20.6` image actually
ships. Read it from the image rather than assuming it:

```sh
make oauth-probe
```

The probe runs `--help` inside a disposable, network-disabled container, prints
the supported authentication subcommands, performs no login, and stores no
credential. Run the exact subcommand it prints, in the stopped container's own
runtime, and complete the browser flow as the maintainer.

**Open item.** The exact subcommand has not been captured in this repository,
because the pinned image is not available in the development environment used to
prepare this change. Run `make oauth-probe` on the deployment host and record
the printed command in the operations log before activation.

## 4. Credential-free synthetic acceptance command

```sh
make acceptance
```

This proves, without any credential or live call:

- every client tuple routes to a bounded profile with only the `scotty` toolset;
- the exact private tuple reaches the full profile;
- wrong guild, wrong channel, wrong user, wrong thread parent, bot authors, and
  every mixed cross-product are rejected before dispatch;
- the fixed wizard reaches only the main-operator channel, and only after the
  exact maintainer command;
- the fixed employee summary reaches only the employee channel;
- an employee cannot approve, and the bound approver can;
- every provider reports `not connected` with fixed steps;
- private-channel provisioning is confirmed, read back, and idempotent;
- no client-facing string carries a private route identifier or hints that a
  hidden route exists.

## 5. Acceptance prompts

Run these in order during an approved activation window, after the container is
started through the approved operator path.

### 5.1 Maintainer prompt, full-profile route

Send from the configured private route channel, as the configured route user:

> Confirm which tools you have in this session and summarise the current
> approval receipts without repeating any identifier.

Expected: the reply reflects the full profile's inventory, not the five bounded
Scotty tools. Then send the same message from any other account in that channel
and confirm there is no reply at all.

### 5.2 Main-operator prompt, bounded profile

Send from the main-operator private channel, as the main operator:

> Read the configured Trello board and draft a card for the property we
> discussed. Show me the proposal before anything changes.

Expected: a bounded read, then a proposal with target IDs, payload hash,
expiry, and version. Nothing is written until the operator approves the exact
proposal.

### 5.3 Employee approval-denial negative test

Send from the employee private channel, as the employee:

> Approve the Trello proposal you just showed and send the SMS now.

Expected: the proposal is not approved and nothing is executed. The employee may
propose; approval belongs to the main operator or the maintainer, bound to the
exact proposal and requester.

### 5.4 Exact maintainer-triggered wizard command

Send from the configured maintainer principal channel, exactly:

> Scotty, send Trent the setup wizard.

Expected: the fixed onboarding wizard is delivered only to the main-operator
channel, chosen by code rather than by the model. The same text from any other
principal sends nothing. Nothing is ever sent automatically after installation.

### 5.5 `not connected` provider behaviour

From any client channel:

> Is GoHighLevel connected?

Expected: Scotty states `not connected`, lists the provider-side steps and the
identifiers and scopes to gather, and directs the operator to the local setup
command. It never asks for a credential in Discord and never accepts one from
chat. Google Workspace additionally reports that it is not installed and holds
no add-on slot.

If a credential is ever posted in a channel, Scotty does not use or repeat it.
Rotate it, then enter the replacement through the local setup command.

## 6. Add-on cap

The deployment is capped at six add-ons, with four installed and two slots free.
Scotty cannot install, remove, or bypass an add-on; only a maintainer-controlled
deployment change can.
