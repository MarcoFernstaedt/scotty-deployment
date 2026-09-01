# Connecting Google Workspace on a headless server

Scotty runs on a VPS with no browser and no desktop session, so it never tries
to open one. Consent happens in a browser Trent already has, and only the two
ends of the exchange touch the server.

Nothing in this document is a live step for development. No credential, token,
authorization code, client file, or account identifier belongs in Git.

## What Google needs first

1. A Google Cloud project for this deployment. One project and one Desktop
   client serve both client users; each user consents separately with their own
   Google account.
2. These APIs enabled on it: Gmail, Google Calendar, Google Drive, Google Docs,
   Google Sheets, People.
3. An OAuth consent screen. While it is in testing, the Workspace account must
   be listed as a test user, or consent fails with `access_denied`.
4. An OAuth client of type **Desktop app**. Its JSON contains the client id and
   client secret.

The scopes Scotty requests are fixed and checked on the way back in:
`openid`, `email`, and the Gmail modify, Calendar, Drive, Docs, Sheets and
Contacts product scopes. A narrower grant is refused rather than used, so a
partial consent never leaves Scotty half-connected. There is no Admin SDK
scope, no directory scope, and no `https://mail.google.com/` permanent-delete
scope.

## Importing the client

Download the client JSON from the Cloud console onto the server. The first time
an account is connected, local setup asks for its path — the path is not secret,
so it is typed visibly — and imports the file, as root, into

    /srv/Scotty/operator/google-oauth-client.json    root:root 0600

It is validated on import: it must be an installed/Desktop client, and its
authorization and token endpoints must be Google's own. A `web` client, or a
client pointing anywhere else, is refused at import rather than at consent time.
Once the protected copy exists, setup never asks for the path again; delete that
file to import a different client.

## Consent, without a browser on the server

Local setup prints the exact authorization URL and publishes the same
non-secret URL for Scotty to show Trent in his own private channel:

    /srv/Scotty/data/scotty/google-consent.<role>.json   10000:10000 0600

That file holds only the authorization URL, the redirect address, and the
scopes. The client secret and the PKCE verifier stay in the root-owned files,
so showing the URL in Discord exposes nothing that could complete the flow.

It lives exactly as long as the attempt does. When the attempt ends — whether
consent succeeded, failed, or was abandoned — the file is removed, because its
verifier is gone and the URL can no longer be completed by anyone. Scotty
therefore never shows a stale link that looks live.

The user whose account it is opens the URL as that Workspace account and approves it. The
browser then redirects to

    http://localhost:8765/oauth2/callback?state=...&code=...

Nothing is listening there, so the page does not load. That is expected: the
address bar now carries the authorization code.

## Returning the code

An authorization code is credential material, so it does not go through
Discord — Scotty cannot accept credentials there at all. The full redirect
address is given to the operator, who pastes it into local setup at a hidden
prompt. It is never echoed, printed, logged, or placed in a command argument.

Setup then exchanges it, checks the granted scopes match exactly, verifies
which account actually consented, and refuses if that is not the configured
Workspace account.

## Where the tokens live

Consent produces two halves, and they are kept in two places.

**The long-lived half — with root, outside every mount.** The OAuth client id,
the client secret, and each user's refresh token go to the root-owned
credential broker's store:

    /var/lib/scotty/credentials.json                          0:0 0600

The client id and secret belong to the deployment and are held once under the
shared identity. A refresh token is one person's consent and is stored under
that person, so no lookup from one user's slot reaches the other's. None of it
is bind-mounted into the runtime container, and no broker operation returns any
of it — the sweep in `tests/test_google_isolation.py` tries every operation on
the runtime's own socket and reads every byte that comes back.

**The short-lived half — in the container.** Each client user has their own
record of who they connected as and the current hour's access token:

    /srv/Scotty/data/scotty/google-oauth.main_operator.json   10000:10000 0600
    /srv/Scotty/data/scotty/google-oauth.employee.json        10000:10000 0600

Each holds the granted scopes, the bound account, and one access token. There
is no field in that record for a refresh token or a client secret; a record
written by an earlier version, which did carry them, is refused rather than
read. The file name is derived from the fixed role slug, never from anything a
model or a message can influence, and a user with no record of their own is
told to connect rather than falling through to the other user's. The runtime
refuses to read a record that is group- or world-readable.

Mode `0600` separates users; it does not separate a plugin, a tool call and a
maintainer session that all run as the same account. That is why the material
that outlives the hour is not there.

**What the container can still do with what it holds.** An access token is a
bearer credential and is good for about an hour. That is the remaining
exposure, and it is stated rather than implied: a compromise of the runtime
gets an hour of that user's Workspace, not a grant that outlives every password
change. Closing the rest means every Google call becoming a declared broker
operation, which is not done yet.

**Refresh.** A couple of minutes before the access token expires, the runtime
asks the broker for another, citing the Discord message it is acting on. The
broker resolves who is asking from that citation, makes the exchange with the
material it holds, and returns only the new token and its expiry. A refresh
never widens scope or rebinds the account, and a failure leaves the previous
binding exactly as it was. If Google rotates the refresh token, the broker
keeps the new one; the runtime is not involved. Consent is a one-time step;
expiry alone never means "not connected".

**Revoke and reconnect.** Revoke access from that Google account's own security
settings, then delete that user's token record and rerun local setup to consent
again. Deleting the record alone leaves the grant live on Google's side, and
revoking one user's access never affects the other's.

## When it goes wrong

- **`access_denied`** — the consent screen is in testing and the account is not
  a test user, or consent was declined. Add the account, then retry.
- **Expired or already-used code** — authorization codes are short-lived and
  single-use. Nothing is stored on failure; rerun setup for a fresh URL.
- **Scope mismatch** — a checkbox was cleared on the consent screen. Rerun and
  approve every requested scope; a partial grant is refused.
- **Wrong account** — consent was completed as a different Google account than
  the one configured. Sign out, rerun, and approve as the configured account.
- **Redirect mismatch** — the pasted address must be the loopback redirect for
  this attempt. A different host, a stale attempt, or a missing code is refused.

## After connecting

Verification is reads only. Scotty confirms the token is bound to the right
account and scope set, and representative bounded reads are exercised against
synthetic responses in the test suite. No live write, send, or provider mutation
is performed during development or verification.
