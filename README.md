# Nutrix full-stack starter

This is a separate application in `nutrix-platform`; it does not replace or modify `nutrix.html` or `nutrix-app.html`.

## Included

- Patient/family and professional sign-in using an international-format mobile number and password.
- SMS one-time-password verification through Twilio Verify. Local development can either use a one-time code printed **only in the server terminal** or explicitly skip phone verification; neither mode is suitable for real accounts.
- Separate patient and caregiver dashboards.
- Nurse/caretaker onboarding with mobile verification followed by an administrator credential review.
- Patient-owned care notes and emergency contact, with details limited to the patient and an assigned caregiver.
- Care booking with server-created Razorpay orders, server-side signature verification, and a webhook.
- SQLite storage, scrypt password hashing, opaque server-side sessions, HTTP-only cookies, request limits and role checks.

## Run locally on Windows

1. Install Python 3.10 or later.
2. In PowerShell, go to this folder and create a local configuration:

   ```powershell
   Copy-Item .env.example .env
   $adminKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
   (Get-Content .env) -replace '^ADMIN_REVIEW_KEY=.*$', "ADMIN_REVIEW_KEY=$adminKey" | Set-Content .env
   py -3 server.py
   ```

3. Open `http://127.0.0.1:8000`. To skip OTP in the local demo, set `SKIP_PHONE_VERIFICATION=true` in `.env` (only works with `APP_ENV=development`). New accounts will be marked as having an unverified phone number. To test OTP instead, set it to `false`, keep `ALLOW_LOCAL_OTP=true`, and use the randomly generated code printed in the server terminal. Do not expose the local server publicly.
4. Open `http://127.0.0.1:8000/admin-review.html` and enter the `ADMIN_REVIEW_KEY` from `.env` to review nurse/caretaker credentials.
5. For actual SMS delivery, configure the three Twilio Verify variables. For test payments, configure the Razorpay test key ID and secret. Configure the Razorpay webhook secret and register `/api/payments/webhook` with Razorpay. Do not use production credentials until deployment security and payment operations have been reviewed.

`run-local.bat` starts the development server after `.env` exists. Keep `.env` private.

## Production checklist

The included SQLite database is for local development only: health profile fields are **not encrypted at rest**. Do not store actual patient information in this starter or deploy it as a clinical record service until a production database with encryption at rest, TLS, backups, access reviews, retention/deletion controls, audit monitoring, operational recovery, and jurisdiction-specific privacy/compliance review are in place. Run behind a trusted HTTPS reverse proxy; set `APP_ENV=production`, require Twilio verification, keep `SKIP_PHONE_VERIFICATION=false`, disable local OTP, use a production-grade shared rate limiter, and protect every provider/admin secret. The reference verification workflow records a professional credential number; administrators must independently verify it with the issuing body. It is not identity-proofing or automatic licence validation.

Prices and services in `server.py` are example INR amounts. Update them only after deciding applicable taxes, cancellation/refund terms, caregiver pay, and who is the merchant of record. The starter does not implement caregiver payouts, refunds, tax invoices, SMS costs, or a production admin identity provider.
