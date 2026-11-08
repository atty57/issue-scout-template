# Draft delivery through scouts.email

Users supply only the email address where they want their issue draft:

```json
{"email": "you@example.com"}
```

Add that field to the rest of `profile.json`, or use `SCOUT_RECIPIENT_EMAIL` as
an environment variable / GitHub repository Variable. The sender defaults to
`noreply@scouts.email`. No user mailbox password or Resend key is requested.
An onboarding app can collect this one field and save it with the user's profile;
this repository currently exposes configuration rather than a signup UI.

## Architecture

```text
User's Scout workflow -- short-lived GitHub OIDC token --> operator's HTTPS relay
                                                        --> Resend --> user's inbox
```

The shared Resend key lives only in the operator's hosted relay. It must not be
distributed in an installer, public source, or generated user repository. The
relay is a separate WSGI service in `email_relay.py`; it does not load profiles,
run models, or require the engine's dependencies. The operator pays for the
relay hosting and any Resend usage. This replaces the original plan's assumption
that all delivery runs on users' own accounts without maintainer infrastructure.

Store the real key in the relay host's server-side secret manager as
`RESEND_API_KEY`. The public `.env.relay.example` contains only an empty setting,
not a credential. User configuration and the Actions workflow do not accept or
receive this key. Relay responses return a fixed acknowledgement or sanitized
error, never provider response bodies or authorization headers. Local `.env` and
`.env.*` secret files are excluded from Git; only blank example files are shared.

The included Actions workflow has `id-token: write` permission. At send time,
Scout obtains a short-lived token addressed to the relay URL. The relay verifies
the signature, issuer, audience, expiry, and required workflow claims using
GitHub's public signing keys. It accepts only an enrolled repository ID and owner
ID, the configured branch, the `daily.yml` workflow, and `schedule` or
`workflow_dispatch` events. The recipient must match that repository's enrollment.
The API cannot select another sender or send to arbitrary recipients.

## Operator setup

1. Verify `scouts.email` in Resend and configure the DNS records Resend provides.
2. Create a sending API key and store `RESEND_API_KEY` only in the relay host's
   secret settings. Do not paste the key into chat or commit it to Git.
3. Host the WSGI service behind HTTPS. Choose its full `/send` URL; the domain
   used for email does not automatically create or host this endpoint.
4. Set the relay's environment using `.env.relay.example` as a reference:

   | Setting | Value |
   |---|---|
   | `RESEND_API_KEY` | Operator's private Resend sending key |
   | `SCOUT_EMAIL_FROM` | `noreply@scouts.email` by default; a plain address on a verified domain |
   | `SCOUT_EMAIL_RELAY_URL` | Full deployed HTTPS URL ending in `/send`; also the expected OIDC audience |
   | `SCOUT_EMAIL_RECIPIENTS` | JSON enrollment map described below |

5. Publish the relay URL to users' repositories as `SCOUT_EMAIL_RELAY_URL`.
   When the app is built, it should set this automatically rather than ask users.
6. Enroll each user's repository and confirmed destination email. Repository
   creation returns its immutable `id` and the owner's `id`; those values can
   also be retrieved from GitHub's repository API. No enrollment endpoint or
   automated email verification UI is included yet, so enrollment is an operator
   step for now. Confirm ownership of the destination before enrolling it.

Example enrollment, keyed by immutable repository ID:

```json
{
  "12345678": {
    "owner_id": "9876",
    "email": "you@example.com",
    "ref": "refs/heads/main"
  }
}
```

`ref` defaults to `refs/heads/main`. Set `refs/heads/master` explicitly for a
repository using that branch. Keep `owner_id` to prevent a transferred repository
from retaining access. Update enrollment when a user changes their destination.
An unregistered repository or mismatched recipient receives HTTP 403 and retains
its draft as a workflow artifact.

Install and run on a suitable Linux host with Python 3.12:

```bash
python -m pip install -r requirements-email-relay.txt
# Configure the environment above using your host's secret/environment settings.
gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 4 --timeout 45 'email_relay:create_app()'
```

Put HTTPS in front of port 8080. The relay needs outbound access to GitHub's OIDC
signing-key endpoint and `api.resend.com`. Do not put tokens or message bodies
in access logs. No deployment or real email send is performed by the test suite.

## Delivery behavior

The relay sends the existing plain-text report, HTML alternative, and safe patch
attachment. The sender is fixed by the operator. Drafts and patches are saved
locally before delivery begins.

Transient network errors, HTTP 429, and server errors are retried. Each workflow
run/attempt has one stable Resend idempotency key. Repeated requests, including
ambiguous timeout retries, use that same key; Resend retains idempotency keys for
24 hours. A changed draft in the same run can be rejected as an idempotency
conflict rather than producing a second email. Tokens expire independently.

A verified fix remains completed if delivery fails, so Scout does not solve the
same issue again just to resend mail. Download the run artifact to recover it.
Without an email or relay URL, Scout previews the message and saves the report.
Local CLI runs do not have GitHub Actions identity; they save the report instead
of sending through this workflow-only relay. Desktop sign-in / local relay
authentication remains future app work.

The relay accepts at most a 2 MB request, 500 KB each of text and HTML, and one
base64-encoded patch up to 750 KB decoded. Unsupported or oversized drafts remain
available through the saved report and patch.

See [Resend email sending](https://resend.com/docs/api-reference/emails/send-email),
[Resend idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys),
and [GitHub Actions OIDC](https://docs.github.com/en/actions/reference/security/oidc).
