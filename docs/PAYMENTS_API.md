# M-Pesa Suite — Payments API v1

For chatbots (WhatsApp, web chat), online shops and any outside system that needs to
**ask a customer to pay by M-Pesa** and **know when the money arrived**.

- Base URL: `https://<your-erp>/api/method/frappe_mpsa_payments.gateway.v1.<endpoint>`
- Works on Frappe/ERPNext v15 and v16.
- Body: JSON (`Content-Type: application/json`) or form-encoded.

## 1. Get credentials

In the ERP: **Mpesa API Client → New**
- **Client Name**, e.g. `WhatsApp Bot`
- **Prompt On**: the MPesa Express settings, e.g. `208911`
- Allowed actions, maximum amount, optional allowed IPs and rate limit
- **Webhook URL** (HTTPS) to be told when payments finish

On save you see the **API key**, the **API secret** and the **webhook signing secret** once.
Store them in your bot or shop's secret store. Send the key and secret on every call:

```
X-Mpesa-Client-Key: mpk_xxxxxxxxxxxxxxxxxxxxxxxx
X-Mpesa-Client-Secret: mps_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 2. Response shape

Success, HTTP 200:
```json
{"message": {"ok": true, "payment": { ... }}}
```

Failure, with an HTTP error status:
```json
{"message": {"ok": false, "error": {"code": "invalid_phone", "message": "phone must be a Kenyan mobile number, e.g. 0712345678."}}}
```

A `payment` object:
```json
{
  "id": "MPI-26-10-000123",
  "order_ref": "WA-5521",
  "status": "prompted",
  "amount": 1500,
  "currency": "KES",
  "phone": "2547****5678",
  "receipt": null,
  "paid_amount": null,
  "paid_at": null,
  "payer_name": null,
  "failure_reason": null,
  "expires_at": "2026-10-18T15:20:00",
  "pay_url": "https://erp.example.com/pay/3f2c…",
  "paybill": {"business_number": "208911", "account_number": "MPI-26-10-000123"},
  "reference": {"doctype": "Sales Order", "name": "SAL-ORD-2026-01234"},
  "metadata": {"chat_id": "2547…"}
}
```

`status` is one of `created`, `prompted`, `paid`, `failed`, `cancelled`, `expired`.
- `failed` means this prompt failed (for example the customer cancelled it). You may resend the prompt until the payment expires.
- `paid`, `cancelled` and `expired` are final.

The payment `id` is also the paybill **account number**. If the customer ignores the prompt
and pays by paybill with that account number, the payment still completes.

## 3. Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `create_prompt` | POST | Create a payment and send the STK prompt now |
| `create_payment_link` | POST | Create a payment the customer completes on the hosted page `/pay/<token>` |
| `get_status` | GET/POST | Current status, checking Safaricom-side results first |
| `resend_prompt` | POST | Send the prompt again (max 5 per payment), optionally to another phone |
| `verify_receipt` | POST | Check an M-Pesa code the customer typed and optionally apply it to a payment |
| `cancel` | POST | Cancel an unpaid payment |
| `list_payments` | GET/POST | Your payments, newest first (`since`, `status`, `limit` ≤ 200) |

### create_prompt
```bash
curl -X POST "$BASE.create_prompt" \
  -H "X-Mpesa-Client-Key: $KEY" -H "X-Mpesa-Client-Secret: $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"amount": 1500, "phone": "0712345678", "order_ref": "WA-5521",
       "idempotency_key": "wa-5521-1", "description": "2 x 1000L tank lids",
       "metadata": {"chat_id": "254712345678"}}'
```

| Field | Notes |
|---|---|
| `amount` | Whole shillings, between 1 and the client's or paybill's limit |
| `phone` | 07…, 01…, 2547…, +254… |
| `order_ref` | Your reference, 140 characters max. `get_status` can look payments up by it |
| `idempotency_key` | Recommended. Repeating a call with the same key returns the same payment instead of prompting twice |
| `reference_doctype` / `reference_name` | Optional, needs *Link Payments to Sales Orders / Invoices*. Use `Sales Order` or `Sales Invoice`; the money is then booked against that document |
| `callback_url` | Optional per-payment webhook. Its host must be in the client's allowed hosts |
| `metadata` | Any JSON. It is echoed back in responses and webhooks |

### create_payment_link
Takes the same fields (phone optional), plus `return_url` (where the page sends the customer
after paying) and `expires_in_hours` (default from client, max 168). Send the customer the
`pay_url`.

### get_status
```bash
curl "$BASE.get_status?order_ref=WA-5521" -H "X-Mpesa-Client-Key: $KEY" -H "X-Mpesa-Client-Secret: $SECRET"
```

### verify_receipt ("I have paid, the code is UJIB2AMYHE")
```bash
curl -X POST "$BASE.verify_receipt" -H "X-Mpesa-Client-Key: $KEY" -H "X-Mpesa-Client-Secret: $SECRET" \
  -d receipt=UJIB2AMYHE -d order_ref=WA-5521
```
Returns `found`, `amount`, `paid_at`, `payer_name` and `claimed`.
- Only receipts on this company's paybills and tills are found.
- When `intent_id` or `order_ref` is given, the payment becomes `paid` only if the receipt
  - is not booked or claimed already,
  - is from the last 72 hours and not older than the payment,
  - covers the amount.

## 4. Webhooks

The webhook is sent once a payment becomes `paid`, `failed`, `expired` or `cancelled`:

```
POST <webhook_url>
Content-Type: application/json
X-Mpesa-Event: payment.succeeded        (or payment.failed / payment.expired / payment.cancelled)
X-Mpesa-Delivery: 8f1c2…                (same value on retries; use it to ignore duplicates)
X-Mpesa-Signature: t=1792345678,v1=5d41402abc…

{"id": "8f1c2…", "event": "payment.succeeded", "created": 1792345678, "data": { …payment… }}
```

- Answer with any 2xx within 15 seconds.
- Failed deliveries are retried after 1, 5, 15, 60, 180, 720 and 1440 minutes. Every attempt is
  listed under **Mpesa Webhook Delivery** in the ERP, where it can be re-sent.
- Redirects are not followed.

**Verify every webhook.** Compute HMAC-SHA256 with the webhook signing secret over
`"<t>.<raw body>"` and compare it with `v1`. Reject timestamps more than 5 minutes old.

Python:
```python
import hashlib, hmac, time

def verify(secret: str, raw_body: bytes, header: str, tolerance=300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    ts = int(parts["t"])
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))
```

Node.js (Express, raw body):
```js
const crypto = require("crypto");
function verify(secret, rawBody, header, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  const ts = Number(parts.t);
  if (Math.abs(Date.now() / 1000 - ts) > tolerance) return false;
  const expected = Buffer.from(crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex"));
  const given = Buffer.from(parts.v1 || "");
  return given.length === expected.length && crypto.timingSafeEqual(expected, given);
}
```

## 5. Typical chatbot flow

1. The customer orders in chat. The bot calls `create_prompt` with an `order_ref` and `idempotency_key`
   and replies "Check your phone for the M-Pesa prompt".
2. On `payment.succeeded` the bot confirms with the receipt number. Without webhooks, poll
   `get_status` every few seconds for up to 2 minutes.
3. On `payment.failed` (cancelled, wrong PIN, no balance) the bot offers to `resend_prompt` or
   sends the paybill details from `paybill`.
4. When a customer pastes an M-Pesa SMS, the bot calls `verify_receipt` with the code and the `order_ref`.

## 6. Error codes

| HTTP | code | Meaning |
|---|---|---|
| 401 | `unauthenticated` | Missing or wrong key/secret, or the client is disabled |
| 403 | `not_allowed`, `ip_not_allowed` | The action is not enabled for this client, or the source IP is blocked |
| 404 | `not_found`, `reference_not_found` | Unknown payment or document |
| 403 | (Frappe error body) | Wrong HTTP method; Frappe rejects it before the API runs |
| 409 | `idempotency_conflict`, `not_payable`, `already_paid`, `receipt_already_used`, `prompt_pending` | State conflict |
| 422 | `invalid_request`, `invalid_amount`, `amount_too_large`, `invalid_phone`, `invalid_receipt`, `invalid_reference`, `invalid_url`, `amount_too_low`, `receipt_too_old`, `receipt_before_order` | Bad input |
| 429 | `rate_limited`, `too_many_prompts` | Slow down; the per-minute limit is set on the client |
| 502 | `prompt_failed` | Safaricom did not accept the prompt; the message says why |
| 500 | `server_error` | Logged in the ERP Error Log |

## 7. Security notes

- Webhook and callback URLs must use https and resolve to public addresses. This is checked when
  a URL is saved and again before every delivery.
- `resend_prompt` is refused for 90 seconds after a prompt, so the customer gets one prompt at a
  time.
- **Deployment:** the per-client IP allow-list and the pay-page rate limits use the client
  address Frappe sees (the first `X-Forwarded-For` entry). Make sure the reverse proxy (nginx)
  overwrites `X-Forwarded-For` instead of appending to a client-supplied one. Otherwise a caller
  can fake its address.
- Keep the API secret on your server. It must never be shipped in a mobile app or a web page.
- The pay page shows only the amount, description, masked phone and paybill details. It is
  rate-limited and allows 3 prompts per minute per payment and 5 per payment in total.
- Payment amounts and links are fixed when created; the pay page cannot change them.
