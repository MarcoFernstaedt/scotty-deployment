---
paths:
  - "assistant/scotty_business/{identity,routing,ingress,discord_policy,progress,guidance,setup_flow,setup,config,policy,runtime}.py"
  - "assistant/scotty_guard/**/*.py"
  - "fixtures/**/*.json"
  - "tests/{test_discord_operations,test_setup,test_setup_flow,test_guidance,test_fixtures}.py"
---

# Identity, routing, branding, and Discord rules

## Product identity

- This is a managed wholesaling assistant. This release supports persona-name
  customization only: a client user names their own assistant, and no client-visible
  string carries a product brand. Renaming the product itself is not implemented — the
  packages, tool names, profiles, paths, commands, container, network and credential
  environment variables are constants. Do not describe the rename plan as a feature.
- “Scotty” is Trent’s personal assistant name, not the product name.
- Mikey has an independent selectable name/persona.
- Product-level identifiers must be neutral and migration-safe. Prepare repository/product rename guidance, but do not rename the repository before acceptance and Marco’s final name decision.
- Client-visible replies, help, errors, setup, progress, schedules, tools, plugins, and fallback paths must not advertise Hermes Agent, Nous Research, OpenClaw, model/provider brands, framework names, plugin names, or internal infrastructure.
- If asked about other agent products, give brief generic factual information when useful; do not provide setup/migration instructions or unsolicited competing-product recommendations.
- Operator documentation remains technically truthful.

## Three profiles

Use one managed runtime and one Discord bot with:

1. Marco’s private full-capability maintainer profile;
2. Trent’s independent Scotty profile;
3. Mikey’s independent selectable-name profile.

Each client user has separate persona, preferences, memory, sessions, context, reminders, private channel, provider mappings, and in-flight work. Enable per-user sessions in approved shared channels. Never cross-route or expose private conversation, memory, credentials, OAuth state, files, drafts, reminders, or personal provider data.

Shared business truth is explicit: approved Trello property boards/lists, GHL records, RentCast results, property/deal entities, shared work channels, announcements/livestream channels, templates, and approved workflows. Add private/shared classification and provenance.

Bind every inbound source before model/session execution to `(platform, guild/tenant, channel or thread parent, user, role, served profile)`. Reject wrong or mixed tuples, bot origins, malformed provenance, unavailable policy, and model-supplied identity/account overrides without revealing hidden routes.

## Marco maintainer route

- Exact private Marco tuple only, in an operator-owned private realm.
- Full native reasoning and broad tools, not Trent/Mikey’s bounded business inventory.
- Isolated from Imperator state and client-visible routes.
- “Full” does not bypass credential confidentiality, cross-client isolation, provider consent, or approvals for money, destructive, credential, public, or new-audience actions.
- Infrastructure operations remain fixed typed owner-authorized management actions; never expose arbitrary root shell, unrestricted Docker/systemd, package installation, arbitrary paths, or secret reads through Discord.

## Discord functional administration

Do not require the literal Discord `Administrator` bit because it bypasses private-channel isolation. Prove functional permissions and typed operations cover:

- messages, replies, bot-owned edits/deletes, reactions, attachments, approved mentions;
- bounded mass mentions, threads/forums, announcements, livestream reminders/progress, scheduled events;
- channel/category create/edit/order/archive and permission setup;
- roles below the bot’s highest role;
- approved moderation and webhook workflows;
- membership/permission readback and coalesced progress updates.

Routine reversible work inside the configured guild is low-friction. Consequence-gate destructive history cleanup, kicks/bans, security-sensitive roles/permissions, bot/app installation, webhook credential changes, public exposure, new external audiences, bulk messaging, unusual mass mentions, and out-of-scope guild operations.

Acceptance must prove required workflows without `Administrator` and prove private-channel isolation survives.