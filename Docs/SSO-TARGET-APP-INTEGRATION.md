# Integrating an app with DWS Hub SSO — step-by-step guide

How to make your application accept single sign-on from **Downstream Hub (DWS Hub)**.

This guide is written from a **real integration** (Cloud Agent Monitoring, Python/Flask)
including every error we hit and what actually fixed it. Follow the steps in order —
most of the pain comes from doing step 2 (URL layout) after step 5 (writing code).

> **Which mode does the Hub use?** The Hub supports OpenID Connect (OIDC). That is what
> this guide covers and what you should use. An older HS256 "POST bridge" mode also
> exists and is documented in **Appendix B** for legacy apps only.

**Reference implementation in this repo:** `Backend/auth/sso.py` (+ `Backend/config.py`).

---

## Table of contents

1. [What kind of SSO this is](#1-what-kind-of-sso-this-is)
2. [Choose your URL layout FIRST](#2-choose-your-url-layout-first-most-important-step)
3. [Confirm the Hub's discovery document](#3-confirm-the-hubs-discovery-document)
4. [Register your app with the Hub admin](#4-register-your-app-with-the-hub-admin)
5. [Configure your app](#5-configure-your-app)
6. [Implement the two routes](#6-implement-the-two-routes)
7. [Handle BOTH login flows](#7-handle-both-login-flows-the-biggest-gotcha)
8. [The token exchange contract](#8-the-token-exchange-contract)
9. [Verify it works](#9-verify-it-works)
10. [Troubleshooting: every error we hit](#10-troubleshooting-every-error-we-hit)
11. [Security checklist](#11-security-checklist)
12. [Appendix A: session cookie rules](#appendix-a-session-cookie-rules)
13. [Appendix B: legacy HS256 bridge](#appendix-b-legacy-hs256-post-bridge)

---

## 1. What kind of SSO this is

The Hub is an **OAuth 2.0 / OpenID Connect provider**. Your app is a **public client**
using **Authorization Code + PKCE**.

| Property | Value | What it means for you |
|---|---|---|
| Flow | Authorization Code + **PKCE (S256)** | You send a code challenge; no secret needed |
| Client type | **Public** (`token_endpoint_auth_methods: ["none"]`) | **There is NO `client_secret`.** Don't ask for one |
| id_token signing | **RS256** | Verify with the Hub's **public JWKS**, not a shared secret |
| Scopes | `openid`, `profile`, `email` | Request `openid email profile` |
| Response type | `code` | |

**Endpoints** (all discoverable — see step 3):

| Purpose | Path |
|---|---|
| Discovery | `/api/sso/.well-known/openid-configuration` |
| Authorize (browser) | `/api/sso/authorize` |
| Token (server-to-server) | `/api/sso/token` |
| JWKS (public keys) | `/api/sso/jwks` |

> **Key mental model:** the *browser* talks to `/authorize`; your *backend* talks to
> `/token` and `/jwks`. Both network paths must work (see step 5, "Network").

---

## 2. Choose your URL layout FIRST (most important step)

This decision drives everything else. **Getting it wrong wastes hours** — we lost most
of a day to it.

The problem: after login, your app sets a **session cookie**. If your UI and your API
live on **different origins**, the browser will only send that cookie when it is marked
`SameSite=None; Secure` — and **`Secure` requires HTTPS**. So:

> ### ⚠️ Over plain HTTP, a separate frontend origin and backend origin CANNOT share a login session.
> Every API call comes back `401` and the user is bounced back to the login page forever.

Pick one of these:

### Option A — Single origin (recommended, works over HTTP)

One hostname serves everything; a reverse proxy routes by path:

```
browser ──▶ http://app.example.com
              ├── /api/*   ──▶ backend  (e.g. 10.0.0.20:5000)
              ├── /auth/*  ──▶ backend
              └── /*       ──▶ frontend (static UI)
```

- Cookie is **first-party** ⇒ `SameSite=Lax`, `Secure=false` works fine on HTTP.
- No CORS needed.
- **Use this for staging / internal domains without TLS.**

Example nginx vhost (write it as a file — this is config, not shell commands):

```nginx
server {
    listen 80;
    server_name app.example.com;

    location /api/  { proxy_pass http://10.0.0.20:5000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; }
    location /auth/ { proxy_pass http://10.0.0.20:5000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; }
    location /      { proxy_pass http://127.0.0.1:3040; proxy_set_header Host $host; }
}
```

> **`proxy_set_header Host $host` is required.** Without it the backend sets the cookie
> for an internal IP instead of your public hostname, and the browser never sends it back.

### Option B — Split origins (requires HTTPS on both)

`app.example.com` (UI) + `api.example.com` (backend), both over **HTTPS**, with
`SameSite=None; Secure=true` and CORS configured for credentials.

### Decision table

| Your situation | Use | Cookie settings |
|---|---|---|
| HTTP only (internal/staging domain) | **Option A** | `SameSite=Lax`, `Secure=false` |
| HTTPS on one hostname | Option A | `SameSite=Lax`, `Secure=true` |
| HTTPS on two hostnames | Option B | `SameSite=None`, `Secure=true` + CORS |
| HTTP on two hostnames | ❌ **impossible** | — |

---

## 3. Confirm the Hub's discovery document

Before writing any code, prove the Hub is reachable and see its real configuration:

```bash
curl -s https://<hub-host>/api/sso/.well-known/openid-configuration
```

Expected (formatted for readability):

```json
{
  "issuer": "https://<hub-host>",
  "authorization_endpoint": "https://<hub-host>/api/sso/authorize",
  "token_endpoint": "https://<hub-host>/api/sso/token",
  "jwks_uri": "https://<hub-host>/api/sso/jwks",
  "response_types_supported": ["code"],
  "subject_types_supported": ["public"],
  "id_token_signing_alg_values_supported": ["RS256"],
  "code_challenge_methods_supported": ["S256"],
  "scopes_supported": ["openid", "profile", "email"],
  "token_endpoint_auth_methods_supported": ["none"]
}
```

Also confirm the keys are published:

```bash
curl -s https://<hub-host>/api/sso/jwks     # expect {"keys":[{"kty":"RSA", ...}]}
```

✅ **Run this from your backend server too, not just your laptop** — the backend needs
this path for the token exchange.

---

## 4. Register your app with the Hub admin

In the Hub: **Admin → Applications**. You need to agree on three things.

### 4.1 Redirect URI(s)

This is the URL the Hub sends the browser back to after login. It must point at
**your backend's callback route**, reachable through your public URL:

```
https://app.example.com/auth/oidc/callback
```

Rules learned the hard way:

- It must match your app's configured value **byte-for-byte** (scheme, host, port, path,
  no trailing slash differences).
- You may register **several** URIs (e.g. one for an IP-based test, one for the real
  domain), but **your app sends exactly one** — see step 5.
- Register the URL that goes through your **reverse proxy / public hostname**, not an
  internal container port. A static-frontend port (e.g. `:3040`) that doesn't proxy
  `/auth/*` will 404.
- HTTP redirect URIs worked for us on an internal domain. Confirm your Hub allows them
  if you have no TLS.

### 4.2 Client ID

The Hub assigns a `client_id` (e.g. `my-app-name`). It is **not secret** — it appears in
browser URLs. You will need it in your config.

### 4.3 Client type

Confirm **public client / PKCE**. There is **no client secret** to request.

### What to collect before leaving this step

| Item | Example | Secret? |
|---|---|---|
| `client_id` | `my-app-name` | No |
| Discovery URL | `https://<hub-host>/api/sso/.well-known/openid-configuration` | No |
| Registered redirect URI | `https://app.example.com/auth/oidc/callback` | No |

---

## 5. Configure your app

Our implementation uses these environment variables:

```ini
# --- DWS Hub OIDC (public client, PKCE — no client secret exists) ---
OIDC_DISCOVERY_URL=https://<hub-host>/api/sso/.well-known/openid-configuration
OIDC_CLIENT_ID=my-app-name
OIDC_REDIRECT_URI=https://app.example.com/auth/oidc/callback
OIDC_SCOPES=openid email profile

# --- Your app's own session signing key (NOT from the Hub — generate it yourself) ---
SECRET_KEY=<openssl rand -base64 48>

# --- Where to send the user after a successful login ---
FRONTEND_URL=https://app.example.com

# --- Cookie mode: see the decision table in step 2 ---
SESSION_COOKIE_SAMESITE=Lax
SESSION_COOKIE_SECURE=false
```

### 🚨 Single-value variables — the #1 source of wasted time

These take **exactly one value**. Comma-joining them produces confusing failures:

| Variable | Wrong | Symptom |
|---|---|---|
| `OIDC_REDIRECT_URI` | `http://a/cb,http://b/cb` | `invalid_grant` from the token endpoint |
| `FRONTEND_URL` | `http://a:3040,http://b` | HTTP **500**, `Port could not be cast to integer value as '3040,http:'` |
| `API_BASE` (frontend) | `https:http://a:3040` | UI stuck on "Loading…", all API calls fail |

Register multiple redirect URIs **on the Hub**, but configure **one** in your app —
the one matching the URL users actually visit.

Also beware **leftover placeholders**. We shipped `https://<hub-host>/...` verbatim once
and got:

```
Failed to resolve '%3chub-host%3e'      # %3c %3e are URL-encoded < >
```

### Network paths required

| From → To | Why |
|---|---|
| browser → Hub | the `/authorize` redirect and login page |
| browser → your app | loading the UI and the callback |
| **your backend → Hub** | **discovery + token exchange + JWKS** (server-to-server) |

Verify the backend path from inside your app's container/host:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<hub-host>/api/sso/.well-known/openid-configuration
```

---

## 6. Implement the two routes

You need exactly two endpoints, both **public** (no login required):

| Route | Purpose |
|---|---|
| `GET /auth/oidc/login` | Start login — redirect the browser to the Hub |
| `GET /auth/oidc/callback` | Receive the code, get tokens, create the session |

We used **Authlib** (Python), which handles discovery, PKCE, `state`/`nonce`, and
id_token verification. Any mature OIDC client library works.

### Client registration

```python
from authlib.integrations.flask_client import OAuth

oauth = OAuth()

def init_oauth(app):
    oauth.init_app(app)
    oauth.register(
        name="dwshub",
        client_id=config.OIDC_CLIENT_ID,
        server_metadata_url=config.OIDC_DISCOVERY_URL,   # discovery does the rest
        client_kwargs={
            "scope": config.OIDC_SCOPES,
            "code_challenge_method": "S256",             # PKCE
        },
        token_endpoint_auth_method="none",               # public client, no secret
    )
```

### Start login

```python
@auth_bp.route("/oidc/login")
def oidc_login():
    return oauth.create_client("dwshub").authorize_redirect(config.OIDC_REDIRECT_URI)
```

A correct authorize redirect looks like this (verify these params if login misbehaves):

```
GET https://<hub-host>/api/sso/authorize
      ?client_id=my-app-name
      &response_type=code
      &redirect_uri=https%3A%2F%2Fapp.example.com%2Fauth%2Foidc%2Fcallback
      &scope=openid+email+profile
      &code_challenge=<...>&code_challenge_method=S256
      &state=<...>&nonce=<...>
```

### Callback

```python
@auth_bp.route("/oidc/callback")
def oidc_callback():
    client = oauth.create_client("dwshub")
    try:
        if request.args.get("code_verifier"):        # IdP-initiated — see step 7
            claims = _idp_initiated_claims(
                client, request.args.get("code"), request.args.get("code_verifier"))
        else:                                        # SP-initiated (we started it)
            token = client.authorize_access_token()
            claims = token.get("userinfo") or {}
    except Exception as exc:
        log.warning("OIDC callback failed: %s: %s", type(exc).__name__, exc)
        return "SSO login failed", 401

    sub = claims.get("sub")                          # stable user id
    email = (claims.get("email") or "").strip().lower()
    if not sub:
        return "Invalid token payload (no subject)", 400

    login_user_by_subject(str(sub), email)           # your own session logic
    return redirect(config.FRONTEND_URL, code=303)   # 303 ⇒ browser does a GET
```

**Use the `sub` claim as the user's primary key**, not the email (emails change).
Allow at least 255 characters for it in your database.

---

## 7. Handle BOTH login flows (the biggest gotcha)

There are **two ways** users arrive, and a stock OIDC library only handles one.

### 7a. SP-initiated — user starts at your app

Your app → `/auth/oidc/login` → Hub → callback. Your library stored `state`, `nonce`,
and the PKCE `code_verifier` in the session, so `authorize_access_token()` just works.

### 7b. IdP-initiated — user clicks your app's tile in the Hub dashboard

**Your `/auth/oidc/login` never runs.** The Hub creates the PKCE challenge itself and
redirects straight to your callback, passing the verifier back to you:

```
GET /auth/oidc/callback?code=<code>&state=<state>&code_verifier=<verifier>
```

Because your session has no stored state, the library fails:

```
authlib.integrations.base_client.errors.MismatchingStateError:
    mismatching_state: CSRF Warning! State not equal in request and response.
```

**Fix:** if `code_verifier` is present in the query string, do the exchange manually.

```python
import requests
from authlib.jose import jwt as jose_jwt

def _idp_initiated_claims(client, code, code_verifier):
    meta = client.load_server_metadata()

    # NOTE: JSON body (not form-encoded) — see step 8.
    resp = requests.post(meta["token_endpoint"], timeout=10, json={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.OIDC_REDIRECT_URI,   # required
        "client_id": config.OIDC_CLIENT_ID,         # public client — no secret
        "code_verifier": code_verifier,
    })
    if resp.status_code != 200:
        # Surface the OAuth error body — it names the real problem.
        raise RuntimeError(f"token endpoint {resp.status_code}: {resp.text[:500]}")

    id_token = resp.json().get("id_token")
    if not id_token:
        raise RuntimeError("no id_token in token response")

    # Verify the signature against the Hub's public keys + check the claims.
    jwks = requests.get(meta["jwks_uri"], timeout=10).json()
    claims = jose_jwt.decode(id_token, jwks, claims_options={
        "iss": {"essential": True, "values": [meta["issuer"]]},
        "aud": {"essential": True, "values": [config.OIDC_CLIENT_ID]},
    })
    claims.validate()      # enforces exp / iat
    return claims
```

> **Security note:** in this flow there is no `state` to check, so the **id_token
> signature is your trust anchor**. Always verify it against the JWKS and validate
> `iss`, `aud`, and `exp`. Never skip verification.

---

## 8. The token exchange contract

`POST /api/sso/token`. We determined these requirements empirically — each wrong guess
returns a different error, which is handy for diagnosis:

| Field | Required | Notes |
|---|---|---|
| `grant_type` | ✅ | `authorization_code` |
| `code` | ✅ | single-use, short-lived |
| `code_verifier` | ✅ | from the Hub (IdP-initiated) or your session (SP-initiated) |
| `redirect_uri` | ✅ | **must match exactly**; omitting it ⇒ `invalid_request` |
| `client_id` | ✅ | public client |
| `client_secret` | ❌ | does not exist |

### ⚠️ Send a JSON body, not form-encoded

This one is easy to miss because virtually every OAuth tutorial uses form encoding:

```bash
# ❌ form-encoded → {"error":"unsupported_grant_type"}
curl -X POST https://<hub-host>/api/sso/token \
     -d 'grant_type=authorization_code&code=x&code_verifier=y&client_id=z&redirect_uri=...'

# ✅ JSON → works (invalid_grant here only because the code is fake)
curl -X POST https://<hub-host>/api/sso/token \
     -H 'Content-Type: application/json' \
     -d '{"grant_type":"authorization_code","code":"x","code_verifier":"y","client_id":"z","redirect_uri":"..."}'
```

A successful response contains an **`id_token`** (RS256 JWT). Decode it *after*
verifying the signature:

| Claim | Meaning |
|---|---|
| `sub` | stable user id — **use this as your key** |
| `email` | user's email |
| `iss` | must equal the discovery `issuer` |
| `aud` | must equal your `client_id` |
| `exp` / `iat` | expiry / issued-at — enforce them |

---

## 9. Verify it works

### Public endpoint checks

```bash
# Your app is reachable through its public URL:
curl -s https://app.example.com/api/health

# The login route builds a proper authorize redirect (302 to the Hub):
curl -si https://app.example.com/auth/oidc/login | egrep 'HTTP/|^location'
```

In that `location` header confirm: `client_id`, `response_type=code`,
`code_challenge_method=S256`, `state`, `nonce`, and a `redirect_uri` matching your
registered value.

### Browser checks (the real test)

1. **SP-initiated:** open your app → click *Sign in with DWS Hub* → authenticate →
   you land back on your app **logged in**.
2. **IdP-initiated:** from the Hub dashboard, click your app's tile → same result.
   **Test this one explicitly** — it exercises the step 7b code path.
3. In DevTools → Application → Cookies, confirm your session cookie exists for your
   app's hostname after the redirect. **If it's missing, see step 2 / Appendix A.**

### Server-side confirmation

Your log should show a successful login, e.g.:

```
INFO auth: OIDC login: user@example.com
```

---

## 10. Troubleshooting: every error we hit

| Symptom | Cause | Fix |
|---|---|---|
| `MismatchingStateError: State not equal in request and response` | IdP-initiated flow (Hub tile); no session state | Implement step 7b (handle `code_verifier`) |
| `{"error":"unsupported_grant_type"}` | Token request sent **form-encoded** | Send a **JSON** body |
| `{"error":"invalid_request"}` | `redirect_uri` **missing** from the token request | Include it |
| `{"error":"invalid_grant"}` | `redirect_uri` doesn't match (often a **comma-joined** value), or code reused/expired | Send exactly one, byte-identical URI; always start from a fresh click |
| `Failed to resolve '%3chub-host%3e'` | Placeholder `<hub-host>` left in `OIDC_DISCOVERY_URL` | Put the real hostname in |
| `ConnectionError` / timeout to the Hub | **Backend** can't reach the Hub (only the browser can) | Open network path / DNS from the backend host |
| HTTP **500**, `Port could not be cast to integer value as '3040,http:'` | `FRONTEND_URL` has **two comma-joined URLs** | Single value |
| Login succeeds in logs, but browser bounces back to the login page | Session cookie was **dropped** — `Secure=true` over plain HTTP, or split origins on HTTP | `SESSION_COOKIE_SECURE=false` + `SameSite=Lax` **and** single origin (step 2) |
| Cookie set for an internal IP, not your hostname | Reverse proxy not passing the original host | Add `proxy_set_header Host $host` |
| UI stuck on "Loading…", API calls fail | Frontend's API base URL malformed (e.g. `https:http://…`) or pointing at an unreachable host | Fix the frontend config to your single origin |
| nginx logs `499` on the callback, no app log line | Backend **hung** (e.g. stalled database) so the browser gave up | Fix/restart the backend; check DB health |
| `/auth/oidc/login` returns **404** | OIDC not configured (app gates these routes when config is absent) | Set all three `OIDC_*` variables |

**General debugging tip:** don't let your HTTP client swallow the error body. We were
stuck on an opaque `400 Bad Request` until we logged `resp.text` — which immediately
said `unsupported_grant_type`.

---

## 11. Security checklist

- [ ] **Verify the id_token signature** against the Hub's JWKS before trusting any claim.
- [ ] Validate **`iss`**, **`aud`** (= your `client_id`), and **`exp`**.
- [ ] Use **PKCE `S256`** (never `plain`).
- [ ] Use the **`sub`** claim as the user key; treat `email` as a display attribute.
- [ ] Session cookies: `HttpOnly`, plus the right `SameSite`/`Secure` for your layout.
- [ ] Prefer **HTTPS everywhere**; if you must run HTTP, keep it internal-only and
      single-origin, and plan to move to TLS.
- [ ] Never log the `code`, `code_verifier`, or raw tokens.
- [ ] Don't ask for or invent a `client_secret` — this is a public client.
- [ ] Do your **own authorization** after authentication: a valid id_token proves *who*
      the user is, not *what* they may do.
- [ ] Generate your app's `SECRET_KEY` yourself; it is unrelated to the Hub.

---

## Appendix A: session cookie rules

| Layout | Scheme | `SameSite` | `Secure` | Works? |
|---|---|---|---|---|
| Single origin | HTTP | `Lax` | `false` | ✅ |
| Single origin | HTTPS | `Lax` | `true` | ✅ |
| Split origins | HTTPS | `None` | `true` | ✅ (needs CORS with credentials) |
| Split origins | HTTP | `None` | `true` | ❌ browser rejects `Secure` on HTTP |
| Split origins | HTTP | `Lax` | `false` | ❌ cookie not sent cross-site |

The last two rows are the trap: login *appears* to succeed (your log shows it) but the
browser never stores or sends the cookie, so the app bounces the user back to login.

---

## Appendix B: legacy HS256 POST bridge

> **Do not use for new integrations.** Documented for older target apps only.

In this mode the Hub auto-POSTs a short-lived HS256 JWT to your app:

- **Method/URL:** `POST` to your configured target URL. If it doesn't contain `/auth/`,
  the Hub appends `/auth/hub` (`https://myapp.example.com` → `…/auth/hub`).
- **Body:** `application/x-www-form-urlencoded`, single field `token=<JWT>`.
- **Signature:** **HS256** using a **shared secret** (`SSO_TOKEN_SECRET`) that must be
  identical on the Hub and your app, obtained from the Hub operator over a secure
  channel (it is never exposed via the Hub UI or API).
- **Claims:** `user_id` (UUID), `email`, `iat`, `exp`. Default lifetime **60 seconds**.

Verification steps: read `token` from the body → verify the HS256 signature with the
shared secret → reject if malformed, wrong signature, or `exp` in the past → map
`user_id`/`email` to a local user and start your own session.

```python
# Python (PyJWT)
payload = jwt.decode(token, key=SSO_TOKEN_SECRET, algorithms=["HS256"], leeway=10)
```

Notes if you maintain such an app:
- The endpoint receives a **cross-site POST**, so it needs a CSRF exemption; the JWT
  signature is the authenticity check.
- Redirect with **303** afterwards so the browser issues a GET carrying the new cookie.
- Because the token is single-use and short-lived, replay risk is limited — but
  enforcing a `jti`/replay table is stronger.

**Why we migrated to OIDC:** no shared secret to distribute or rotate, asymmetric
signatures verified via public JWKS, standard libraries, and support for both
SP-initiated and IdP-initiated login.
