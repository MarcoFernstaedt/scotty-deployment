# Connecting Google Workspace on a headless server

Scotty runs on a VPS with no browser and no desktop session, so it never tries
to open one. Consent happens in a browser Trent already has, and only the two
ends of the exchange touch the server.

Nothing in this document is a live step for development. No credential, token,
authorization code, client file, or account identifier belongs in Git.

## What Google needs first

1. A Google Cloud project for this deployment.
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

The client JSON is imported through local setup, as root, into

    /srv/Scotty/operator/google-oauth-client.json    root:root 0600

It is validated on import: it must be an installed/Desktop client, and its
authorization and token endpoints must be Google's own. A `web` client, or a
client pointing anywhere else, is refused at import rather than at consent time.

## Consent, without a browser on the server

Local setup prints the exact authorization URL and publishes the same
non-secret URL for Scotty to show Trent in his own private channel:

    /srv/Scotty/data/scotty/google-consent.json      10000:10000 0600

That file holds only the authorization URL, the redirect address, and the
scopes. The client secret and the PKCE verifier stay in the root-owned files,
so showing the URL in Discord exposes nothing that could complete the flow.

Trent opens the URL as the configured Workspace account and approves it. The
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

## Where the token lives

    /srv/Scotty/data/scotty/google-oauth.json        10000:10000 0600

It holds the access token, the refresh token, the granted scopes, the bound
account, and the client identity needed to refresh. Scotty refuses to read it
if it is group- or world-readable.

**Refresh.** The access token lasts about an hour and is refreshed in place
from that file, a couple of minutes before it expires. A refresh never widens
scope or rebinds the account, and a failed refresh leaves the previous state
exactly as it was. Consent is a one-time step; expiry alone never means "not
connected".

**Revoke and reconnect.** Revoke Scotty's access from the Google account's
security settings, then delete the token file and rerun local setup to consent
again. Deleting the token file alone leaves the grant live on Google's side.

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
