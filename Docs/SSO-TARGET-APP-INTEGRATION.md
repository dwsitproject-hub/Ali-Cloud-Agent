# SSO: How Target Applications Decode the Token

This document describes how **target applications** (downstream apps that receive users from Downstream Hub via SSO) should receive, verify, and decode the SSO token so that SSO works end-to-end.

---

## 1. How the token is delivered to your app

1. The user clicks an application in the Hub dashboard.
2. The Hub redirects the user to a **bridge page** on the Hub API.
3. The bridge page **auto-POSTs** a form to **your application’s URL** with the token in the request body.

**Contract:**

- **HTTP method:** `POST`
- **Content-Type:** `application/x-www-form-urlencoded` (standard form submit)
- **Body field name:** `token`
- **Body value:** A single-use **JWT string** (the SSO token)

So your app will receive something like:

```
POST /auth/hub HTTP/1.1
Content-Type: application/x-www-form-urlencoded

token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

The Hub uses the **target URL** configured in Admin → Applications for your app. If that URL does not already contain `/auth/`, the Hub appends `/auth/hub` (e.g. `https://myapp.example.com` → `https://myapp.example.com/auth/hub`). Your app must expose a route that accepts this POST and reads the `token` from the body.

---

## 2. Token format and contents

- **Format:** JWT (JSON Web Token), signed with **HS256**.
- **Encoding:** UTF-8. The shared secret is used as a raw UTF-8 byte sequence (no base64 or other encoding of the secret).

### Payload claims (after verification)

| Claim   | Type   | Description |
|---------|--------|-------------|
| `user_id` | string (UUID) | Hub user ID. Use this as a stable identifier for the user in your system (e.g. map to your local user or create one). |
| `email`   | string | User’s email address (lowercase). |
| `iat`     | number | Issued-at time (Unix timestamp, seconds). |
| `exp`     | number | Expiration time (Unix timestamp, seconds). |

**Example decoded payload (JSON):**

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "iat": 1709301600,
  "exp": 1709301660
}
```

- **Default lifetime:** 60 seconds (`exp - iat = 60`). Configurable on the Hub via `SSO_TOKEN_EXPIRY_SECONDS` (env). Your app should **reject tokens with `exp` in the past**.

---

## 3. Shared secret

The token is signed with a **shared secret** that must be the same on both sides:

- **Hub (issuer):** Uses the environment variable `SSO_TOKEN_SECRET`.
- **Target app (verifier):** Must use the **exact same value** to verify the signature.

You must obtain this value securely from the Hub operator (e.g. via a secure channel or secrets manager). Do not expose it in client-side code or in URLs.

- **Secret format:** Arbitrary string (UTF-8). No base64 or hex encoding is applied to the secret; both Hub and target app use the raw string as the HMAC key (in UTF-8 bytes for HS256).

### How to get the SSO_TOKEN_SECRET

**If you run the Hub (Hub operator / admin):**

- The secret is **set by you** in the Hub’s environment. It is **not** generated or shown in the Hub UI or API (by design, to avoid exposing it).
- **Local / development:** In the Hub project root, copy `.env.example` to `.env` and set:
  ```bash
  SSO_TOKEN_SECRET=your-sso-shared-secret-change-in-production
  ```
  Replace the value with a long, random string in production (see below).
- **Production:** Set the `SSO_TOKEN_SECRET` environment variable in your deployment (e.g. server env, Docker env, or your platform’s secrets/config). Use a strong random value; for example:
  ```bash
  # Generate a 32-byte secret (paste into .env or your secrets manager)
  openssl rand -base64 32
  ```
  You can use that output as-is as the secret (both Hub and target apps use the same **exact** string).
- **Sharing with target apps:** Send the secret to each target app team through a **secure channel** (e.g. encrypted message, secrets manager handoff, or in-person). Do not put it in email, docs in version control, or public URLs.

**If you run a target application:**

- You **do not** read the secret from the Hub automatically. There is no API or screen in the Hub that returns `SSO_TOKEN_SECRET`.
- **Obtain the value from the Hub administrator** (the team that runs Downstream Hub). They will set the same secret on the Hub and share it with you via a secure channel.
- Store it in your app’s environment or secrets manager (e.g. `SSO_TOKEN_SECRET` in your `.env` or deployment config). Use it only on the server side to verify the JWT; never expose it to the browser or in client code.

---

## 4. How to verify and decode the token (target app)

1. **Read the token** from the POST body (field name `token`).
2. **Verify signature and expiry** using the shared secret and HS256. Reject the request if:
   - Signature is invalid, or
   - Token is expired (`exp` &lt; current time), or
   - Token is malformed.
3. **Use the payload** (`user_id`, `email`) to identify or create the user in your app and establish a session (e.g. issue your own cookie or JWT).

Do **not** trust the token without cryptographic verification. Do **not** use the token for authorization beyond “this request was signed by the Hub with the shared secret”; do your own authorization inside your app.

---

## 5. Example: Node.js (jose)

If your target app is Node.js, you can use the same library the Hub uses (**jose**):

```js
const { jwtVerify } = require('jose');

const SSO_TOKEN_SECRET = process.env.SSO_TOKEN_SECRET; // same value as on the Hub

async function handleHubPost(req, res) {
  const tokenString = req.body?.token; // from POST body (e.g. express.urlencoded())
  if (!tokenString) {
    return res.status(400).send('Missing token');
  }

  const secret = new TextEncoder().encode(SSO_TOKEN_SECRET);
  try {
    const { payload } = await jwtVerify(tokenString, secret);
    const { user_id, email, exp } = payload;
    if (!user_id || !email) {
      return res.status(400).send('Invalid token payload');
    }
    // Optional: reject if already expired (jose often does this for you)
    if (exp && exp < Math.floor(Date.now() / 1000)) {
      return res.status(401).send('Token expired');
    }
    // Log the user in or create/link account; then redirect to your app
    // e.g. createSession(user_id, email); res.redirect('/dashboard');
    res.redirect('/dashboard');
  } catch (err) {
    return res.status(401).send('Invalid or expired token');
  }
}
```

- **Secret:** Use the same `SSO_TOKEN_SECRET` as the Hub; encode as UTF-8 (e.g. `TextEncoder().encode(SSO_TOKEN_SECRET)` for jose).

---

## 6. Example: Other languages / libraries

- **Python (PyJWT):** Use `jwt.decode(token, key=SSO_TOKEN_SECRET, algorithms=["HS256"])`. Pass the secret as a string (UTF-8); PyJWT will use it as the HMAC key.
- **Java (e.g. jjwt):** Verify with algorithm `HS256` and the shared secret as a byte array (UTF-8).
- **.NET:** Use `JwtSecurityTokenHandler` with `HMACSHA256` and the secret as UTF-8 bytes.
- **PHP, Go, etc.:** Any JWT library that supports HS256; key = shared secret in UTF-8.

In all cases:

1. Verify signature with the **shared secret** (UTF-8).
2. Use algorithm **HS256** only (the Hub does not issue other algorithms).
3. Enforce **exp** (reject if `exp` &lt; now).

---

## 7. Endpoint contract summary (for your app)

| Item | Value |
|------|--------|
| Method | `POST` |
| URL | Your configured target URL (e.g. `https://yourapp.example.com/auth/hub`) |
| Body | `application/x-www-form-urlencoded` |
| Body parameter | `token` = JWT string |
| JWT algorithm | HS256 |
| JWT claims | `user_id` (UUID), `email`, `iat`, `exp` |
| Secret | Same as Hub `SSO_TOKEN_SECRET` (UTF-8) |
| Token lifetime | Default 60 s (Hub: `SSO_TOKEN_EXPIRY_SECONDS`) |

---

## 8. Security checklist (target app)

- [ ] Obtain `SSO_TOKEN_SECRET` securely; store in env or secrets manager.
- [ ] Verify the JWT signature before trusting any claim.
- [ ] Reject tokens with `exp` in the past.
- [ ] Use HTTPS for the endpoint that receives the POST.
- [ ] Do not log or expose the raw token or secret.
- [ ] Map `user_id`/`email` to your own user and session; do not forward the Hub token to browsers unless you have a specific design for it.

Once your app correctly reads the POST body, verifies the JWT with the shared secret, and enforces expiry, SSO from the Hub to your app will work as intended.
