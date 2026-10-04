# Sandbox setup

Everything here is done by hand in the PayPal Developer Dashboard
(https://developer.paypal.com/dashboard/). Provenant never creates PayPal accounts itself.

## 1. Sandbox accounts

Go to **Testing Tools > Sandbox Accounts > Create account**. Create:

| Purpose | Type | Country | Suggested label |
|---|---|---|---|
| Buyer A | Personal | United States | `provenant-buyer-a` |
| Buyer B | Personal | United States | `provenant-buyer-b` |
| Northwind Outfitters (honest) | Business | United States | `provenant-northwind` |
| Bayline Goods (sloppy) | Business | United States | `provenant-bayline` |
| Kestrel Supply (misrepresenting) | Business | United States | `provenant-kestrel` |
| Attacker shop | Business | United States | `provenant-attacker` |
| Provenant operator (liability pool) | Business | United States | `provenant-operator` |

Give each Personal account a PayPal balance (the default sandbox balance is fine). Give the
operator account a large balance, since spike S3 pays out from it.

For Phase 0 spikes S1 and S2 you only need **Buyer A** and **Northwind**.

## 2. REST apps

Go to **Apps & Credentials** (Sandbox toggle on) **> Create App**. Create one app per Business
account above, choosing that account as the app's sandbox business account. Type: Merchant.

For each app, copy the Client ID and Secret into `.env` using the key names in `.env.example`:

```
PAYPAL_MERCHANT_NORTHWIND_CLIENT_ID=...
PAYPAL_MERCHANT_NORTHWIND_CLIENT_SECRET=...
```

On the operator app, open the app settings and make sure **Payouts** is enabled.

## 3. Return URLs

Set `PAYPAL_RETURN_URL` and `PAYPAL_CANCEL_URL` in `.env`. During Phase 0 any HTTPS URL works:
PayPal only redirects the buyer there after approval, and the spike detects approval by polling
the order. Later these point at the Buyer App.

## 4. Run the first spikes

```
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"
python spikes/s1_order_authorize_capture.py --open
python spikes/s2_void_and_idempotent_refund.py --open
```

Each spike prints an approval link (or opens it with `--open`). Log in with **Buyer A**'s sandbox
email and password (from the Sandbox Accounts page) and approve. The spike continues on its own.
A redacted log of every request and response lands in `spikes/out/`.
