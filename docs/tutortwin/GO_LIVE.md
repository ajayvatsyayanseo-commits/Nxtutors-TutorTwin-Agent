# Going live: DNS, Meta templates, and the order to do it in

Everything in this document is a thing **you** have to do in somebody else's
console. It is separated from the code docs for that reason.

Two items have real lead time, so start them first:

| Task | Lead time |
|---|---|
| Meta message templates | **24-48 hours** for approval, sometimes longer |
| DNS propagation + TLS | 15 minutes to a few hours |

---

## 1. DNS

Three subdomains. The admin panel gets its **own** host rather than a path on
the public site, and that is a security decision rather than a tidiness one:
the admin session cookie is scoped to its origin, and the public site loads
Cashfree's checkout SDK, which is third-party JavaScript. On a shared origin an
XSS in that SDK could ride the admin session. Separate origins make the browser
enforce the boundary permanently.

| Host | Serves | Login |
|---|---|---|
| `nxtutortwin.nxtutors.com` | `apps/site` - landing, `/payment`, `/payment/success` | none |
| `tutortwinadmin.nxtutors.com` | `apps/admin` - the control plane | required |
| `api.nxtutors.com` | the FastAPI service - webhooks and APIs | per-endpoint |

### Records

DNS for `nxtutors.com` is hosted at **Cloudflare** (`meadow.ns.cloudflare.com`,
`drew.ns.cloudflare.com`), and all three subdomains run on the existing
**CloudPanel** server - an EC2 instance in Mumbai at `13.127.64.109`, the same
region as the RDS database.

So these are `A` records to that server, not CNAMEs to a managed platform:

| Type | Name | Value | Proxy |
|---|---|---|---|
| A | `nxtutortwin` | `13.127.64.109` | DNS only |
| A | `tutortwinadmin` | `13.127.64.109` | DNS only |
| A | `api` | `13.127.64.109` | DNS only |

**Grey cloud, not orange, at least to begin with.** Cloudflare defaults new A
records to proxied. While proxied, CloudPanel's Let's Encrypt HTTP-01 validation
is far more fiddly, and Meta and Cashfree webhooks reach you through an extra
hop. Get it working on DNS-only, then switch the public site to proxied if you
want the CDN.

If you do switch to proxied later, `TUTORTWIN_TRUSTED_PROXY_HOPS` must change
from `1` to `2` - Cloudflare adds a hop to `x-forwarded-for`, and a mismatch
either throttles every admin as one client or reads a value a client can forge.

Existing `nxtutors.com` and `www.nxtutors.com` records are untouched; these are
three new siblings.

### Verify before moving on

```bash
dig +short api.nxtutors.com          # expect 13.127.64.109
curl -sI https://api.nxtutors.com/healthz | head -1     # expect HTTP/2 200
```

Only issue the Let's Encrypt certificates in CloudPanel once `dig` returns the
right IP - requesting before DNS propagates is the usual reason it fails.

---

## 2. Meta message templates

You need **two** approved templates. This is not optional plumbing: outside
Meta's 24-hour customer service window, a plain text message is rejected with a
400. A subscription confirmation is *always* outside it - the student paid on a
website, they were not mid-conversation - so without an approved template the
confirmation silently never arrives.

Create these at **WhatsApp Manager → Account tools → Message templates → Create
template**.

### Template 1 - `subscription_activated` (required)

This is the one the code sends today. The name must match exactly; it is
`ACTIVATION_TEMPLATE` in `src/tutortwin/services/subscriptions.py`.

| Field | Value |
|---|---|
| **Name** | `subscription_activated` |
| **Category** | `UTILITY` (not Marketing - it is a transaction receipt, and Utility is approved faster and priced lower) |
| **Language** | `English` → code `en` |
| **Header** | none |
| **Footer** | `NXTutors · Reply STOP to opt out` |

**Body** - copy this exactly, including the two `{{1}}` `{{2}}` placeholders:

```
Hi {{1}}, your TutorTwin subscription is active.

I am {{2}}, your study buddy. Send me any question - type it, photograph your homework, send a worksheet PDF, or record a voice note - and I will work through it with you step by step.

Try me now: send a photo of anything you are stuck on.
```

**Sample values Meta asks for** (they reject templates submitted without them):

- `{{1}}` → `Aarav`
- `{{2}}` → `Anita Ma'am`

The code passes them in that order: student name, then tutor name.

### Template 2 - `subscription_expiring` (recommended, not yet wired)

Worth submitting now so it is approved when you want it - renewals are where the
revenue is, and template approval is the slow part.

| Field | Value |
|---|---|
| **Name** | `subscription_expiring` |
| **Category** | `UTILITY` |
| **Language** | `en` |

**Body:**

```
Hi {{1}}, your TutorTwin subscription ends on {{2}}.

Renew here to keep your tutor: {{3}}
```

Samples: `Aarav`, `7 October`, `https://nxtutortwin.nxtutors.com/payment`.

### Things that get templates rejected

- **Marketing language in a Utility template.** "Best tutor in India", "limited
  offer", "buy now" - Meta reclassifies or rejects. Keep it transactional.
- **A URL in the body without it being a sample-able variable.** Either put the
  link in a Button, or make it `{{3}}` with a sample as above.
- **Placeholders that are not sequential.** `{{1}}` then `{{3}}` is rejected.
- **Trailing whitespace or a variable at the very start or end of the body.**
  Meta rejects both.

### After approval

Nothing to deploy. The code sends by name and Meta resolves it, so an approved
template starts working immediately. Check with:

```bash
curl -s "https://graph.facebook.com/v21.0/$WHATSAPP_BUSINESS_ACCOUNT_ID/message_templates?fields=name,status,language" \
  -H "Authorization: Bearer $WHATSAPP_ACCESS_TOKEN" | jq '.data[] | {name, status}'
```

Look for `"status": "APPROVED"`.

---

## 3. Meta webhook

**WhatsApp Manager → Configuration → Webhooks → Edit**

| Field | Value |
|---|---|
| Callback URL | `https://api.nxtutors.com/webhooks/whatsapp` |
| Verify token | whatever `WHATSAPP_VERIFY_TOKEN` says in your `.env` (not repeated here - this file is committed) |

Then **subscribe to the `messages` field**. This is the step most often missed:
the webhook verifies successfully, the green tick appears, and no message ever
arrives because nothing is subscribed.

The handshake is already proven to work - the endpoint echoes the challenge and
returns 403 on a wrong token.

---

## 4. Cashfree webhook

**Cashfree dashboard → Developers → Webhooks → Add webhook**

| Field | Value |
|---|---|
| URL | `https://api.nxtutors.com/webhooks/cashfree` |
| Events | `PAYMENT_SUCCESS_WEBHOOK`, and `PAYMENT_FAILED_WEBHOOK` |
| API version | `2025-01-01` |

The signature is verified with `CASHFREE_SECRET_KEY`, which you already have
set. An unsigned or wrongly-signed callback is dropped without touching the
database.

You do not strictly need the webhook for activation to work - the success page
also asks Cashfree directly - but without it a student who closes the tab
immediately after paying waits until they reopen the page. Configure it.

---

## 5. Environment, per host

**API** (`api.nxtutors.com`):

```bash
TUTORTWIN_ENVIRONMENT=production
TUTORTWIN_PUBLIC_SITE_URL=https://nxtutortwin.nxtutors.com
TUTORTWIN_PUBLIC_API_URL=https://api.nxtutors.com
TUTORTWIN_CORS_ALLOW_ORIGINS=["https://nxtutortwin.nxtutors.com"]
```

On a **server** deployment (CloudPanel) also set:

```bash
TUTORTWIN_DEPLOYMENT_TARGET=server
TUTORTWIN_TRUSTED_PROXY_HOPS=1
```

`serverless` demands R2 and Cloud Tasks because Cloud Run has an ephemeral disk
and throttles CPU between requests, so without them media is written to a disk
that disappears and background work never runs. Neither is true on a server, so
`server` requires neither - the local filesystem persists and jobs dispatch
in-process.

**Public site** (`nxtutortwin.nxtutors.com`):

```bash
NEXT_PUBLIC_API_URL=https://api.nxtutors.com
NEXT_PUBLIC_CASHFREE_MODE=production
NEXT_PUBLIC_SITE_URL=https://nxtutortwin.nxtutors.com
```

`NEXT_PUBLIC_CASHFREE_MODE` **must match** `CASHFREE_ENV` on the API. A
production session id opened in sandbox mode fails with an error that names
neither cause.

**Admin panel** (`tutortwinadmin.nxtutors.com`):

```bash
TUTORTWIN_API_URL=https://api.nxtutors.com
```

---

## 6. Order of operations

1. DNS records for all three hosts. Wait for them to resolve.
2. Submit both Meta templates. **Do this on day one** - approval is the long pole.
3. Deploy the API. Confirm `https://api.nxtutors.com/healthz` returns 200.
4. Point the Meta webhook at it and subscribe to `messages`.
5. Point the Cashfree webhook at it.
6. Deploy the public site and the admin panel.
7. Set `TUTORTWIN_ANTHROPIC_API_KEY` and/or `TUTORTWIN_OPENAI_API_KEY`. **Until
   this is done every reply is "TutorTwin is being set up".**
8. Set `WHATSAPP_SEND_ENABLED=false`, put one real subscription through, and
   watch the logs for `whatsapp_turn_handled` and `subscription_activated`.
9. Flip `WHATSAPP_SEND_ENABLED=true`.

---

## 7. What to watch on the first real payment

These log events, in this order:

```
signup_created           order_id=tt_...
cashfree_order_created   order_id=tt_...
subscription_activated   order_id=tt_... plan_code=PRO ends_at=...
whatsapp_sent            kind=template
```

If `subscription_activated` never appears, the webhook is not reaching you -
check the Cashfree dashboard's webhook delivery log. If it appears but
`whatsapp_sent` does not, the template is not approved yet, and
`whatsapp_send_failed` will carry Meta's reason verbatim in `detail`.

An expired access token and a closed 24-hour window are both HTTP 400 from
Meta, and look identical until you read that field - which is exactly why it is
logged rather than swallowed.
