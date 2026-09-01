---
paths:
  - "assistant/scotty_business/adapters/trello.py"
  - "assistant/scotty_business/{service,runtime,policy,calculations}.py"
  - "tests/**/*trello*.py"
  - "tests/{test_provider_connection,test_policy_and_calculations}.py"
  - "fixtures/**/*"
---

# Wholesaling property-card rules

Implement one versioned canonical property-card schema with field authority, normalization, required/optional fields, provenance, migration, and conflict semantics.

Required typed operations:

- create cards from conversational or structured input;
- apply and maintain approved templates;
- reformat existing cards without losing verified values or provenance;
- parse, normalize, validate, and standardize addresses;
- filter, query, sort, label, move, archive, and bounded batch-review cards;
- detect likely duplicates using normalized address, parcel/provider/property identifiers, and configurable evidence;
- return explainable duplicate confidence and reasons;
- generate deterministic comparison and merge previews;
- preserve conflicting values and require explicit conflict selection;
- merge without silent overwrite;
- read back every Trello mutation before marking it verified;
- reconcile ambiguous effects before retry;
- prevent duplicate cards after timeout, crash, or lost acknowledgement;
- preserve actor, source, source ID, retrieval time, prior revision, changed fields, payload hash, and redacted receipt;
- provide bulk dry-run with exact affected-card count and diff.

Gate destructive archive/delete, large batches, unusual destinations, public exposure, and consequential changes. Presentation preferences may differ per user, but stored shared property truth remains canonical.

## Declarative workflow builder

Allow Trent and Mikey to create, preview, revise, activate, pause, and retire wholesaling workflows composed only from installed approved operations.

Every workflow defines owner/actors, purpose, trigger/input, field authority/conflict rules, ordered operation IDs, object/board/location/channel/recipient/rate limits, approval class, schedule/timezone/quiet hours, retries/circuit breaker/stop rule, idempotency/duplicates/reconciliation/compensation, privacy/retention, client wording, and synthetic acceptance examples.

Conversation cannot add integrations, credentials, code, tools, plugins, MCP, models, destinations, network routes, or infrastructure authority. Those become maintainer proposals.