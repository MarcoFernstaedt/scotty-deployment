# Renaming this product after acceptance

This deployment is white-labelable. "Scotty" is the name of Trent's own
assistant, and Mikey names his separately; neither is the product's name. The
repository is still called `scotty-deployment` because renaming it before
acceptance would break the operator's installed paths, the pinned smoke
contract, and every checksum in `SHA256SUMS` for no benefit.

Nothing here is executed by this mission. It is the plan to run once Marco has
chosen a name and the candidate has been accepted.

## What is already neutral

The client-visible surface carries no product brand at all. A client sees only
their own assistant's name, which comes from configuration or from their own
choice:

- the system prompt section addresses the reader's own assistant and tells the
  model to read the name from `scotty_read` `status`;
- the onboarding wizard and the employee summary are rendered per reader;
- the status reply returns the caller's own assistant name;
- provider guidance, refusals, and tool descriptions name no product.

`make acceptance` fails if any of those strings names a framework, a model
provider, or the infrastructure underneath.

## What still carries the old name, and why it is safe

These are operator-facing or on-disk identifiers. They are technically truthful
and never shown to a client:

| Identifier | Where |
| --- | --- |
| `scotty_business`, `scotty_guard`, `scotty_broker` | Python packages |
| `scotty_read`, `scotty_propose`, `scotty_approval`, `scotty_reminder`, `scotty_calculate` | tool names |
| `scotty-maintainer`, `scotty-main-operator`, `scotty-employee` | served profiles |
| `/srv/Scotty`, `/usr/local/lib/scotty`, `/run/scotty` | installed paths |
| `scotty-start`, `setup-scotty`, `scotty-credential-broker` | commands |
| `scotty`, `scotty-egress` | container and network |
| `SCOTTY_*` | credential environment variables |

## The order to change them in

Each step is separately reviewable and separately reversible.

1. **Choose the name.** Confirm the package slug, the command names, and the
   installed root with Marco before touching anything.
2. **Rename the repository** on GitHub. GitHub redirects the old URL, but
   update the canonical remote in the contract and in `README.md` in the same
   change.
3. **Rename the Python packages and tool names** in one commit, including the
   plugin manifests, the installer's file inventory, the profile toolset
   allowlist, and every test that names them. Tool names are part of the
   model-visible contract, so this changes the sessions' tool inventory: plan it
   with a stopped container.
4. **Rename the installed paths and commands.** This is an operator migration,
   not a code change alone: the new installer must move `/srv/Scotty`,
   `/usr/local/lib/scotty`, and `/run/scotty`, restage both client profile
   homes, and rewrite the systemd units and the Compose mounts. Keep the old
   paths readable until the new deployment has passed a start and a health
   check, then remove them.
5. **Rename the credential environment variables** last, accepting both names
   for exactly one release so a rollback does not lose a configured provider.
6. **Regenerate** the package, `SHA256SUMS`, and the pinned smoke evidence, and
   rerun every governing gate against the renamed candidate.

## What must not change with the name

- Trent's assistant stays "Scotty" unless Trent says otherwise; the product name
  and a user's assistant name are separate settings.
- The maintainer route, the profile separation, and the per-user provider
  identities are unaffected by naming and must be re-proven after the rename.
- No client-visible string gains a product brand in the process.
