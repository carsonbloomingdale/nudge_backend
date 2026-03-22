# Authentication — SPA (cookies + JWT)

**One-liner:** The SPA is **cookie-authenticated** (`credentials: 'include'` / `withCredentials: true`); **CORS must list real origins** (not `*`) with **`Access-Control-Allow-Credentials: true`**; **`GET /auth/me`** returns the session user; **task + enrich + suggestion** routes require a valid **access** JWT (cookie or `Authorization: Bearer`).

---

## CORS + credentials (required for cookies)

1. Set **`CORS_ORIGINS`** to a **comma-separated list** of SPA origins, e.g.  
   `https://app.example.com,http://localhost:3000`
2. **Do not** use `CORS_ORIGINS=*` with cookies — browsers will not send credentials, and this API turns off `allow_credentials` when origin is `*`.
3. The API sets **`Access-Control-Allow-Credentials: true`** whenever origins are explicit.

Frontend: every API call must use **`credentials: 'include'`** (fetch) or **`withCredentials: true`** (axios).

### Fixing “CORS / blocked by CORS policy” errors

The `Origin` header must match **exactly** (after we trim spaces and a trailing `/` on your configured values):

| Browser sends | Put in `CORS_ORIGINS` |
|---------------|------------------------|
| `http://localhost:5173` | `http://localhost:5173` (not only `:3000`) |
| `http://127.0.0.1:5173` | `http://127.0.0.1:5173` (`localhost` ≠ `127.0.0.1`) |
| `https://myapp.vercel.app` | that exact URL, **https** not http |

1. Open **DevTools → Network**, click the failing request, check **Request headers → `Origin`**.
2. Add that **exact** value to **`CORS_ORIGINS`** on Koyeb (comma-separated if you have several).
3. For many Vercel preview URLs, set **`CORS_ORIGIN_REGEX`** to a **full-match** pattern, e.g. `https://.*\.vercel\.app` (escape dots in regex).
4. If preflight returns **400** with body **`Disallowed CORS origin`**, the origin is still not allowed — fix the list or regex.

---

## Cookie names & production

| Cookie | Default name | Env override |
|--------|----------------|--------------|
| Access JWT | `access_token` | `AUTH_ACCESS_COOKIE_NAME` |
| Refresh JWT | `refresh_token` | `AUTH_REFRESH_COOKIE_NAME` |

**Production**

- **`COOKIE_SECURE=true`** — cookies are HTTPS-only.
- **`COOKIE_SAMESITE`**
  - Same registrable domain as API (typical reverse proxy): **`lax`** or **`strict`** is often enough.
  - **Different sites** (e.g. SPA on `app.example.com`, API on `api.example.com`): use **`COOKIE_SAMESITE=none`** and **`COOKIE_SECURE=true`**.
- **`AUTH_COOKIE_DOMAIN`** (optional) — e.g. **`.example.com`** so the browser sends cookies to both `https://app.example.com` and `https://api.example.com`. Only set when **SPA and API share a parent registrable domain**. If SPA is **`nudgeweb.app`** and API is **`*.koyeb.app`**, leave **`AUTH_COOKIE_DOMAIN` unset** — the cookie is still stored for the **API host** and sent on `fetch(apiUrl, { credentials: 'include' })` **when** `SameSite=None` + `Secure` are set (see below). Browsers may still restrict **third-party** cookies; if login works but `/tasks/` returns **401**, cookies often aren’t sent on cross-site requests.

### Example: `nudgeweb.app` (SPA) + Koyeb API (`https://your-app-….koyeb.app`)

Set these on the **API** (Koyeb env):

| Variable | Example / value |
|----------|------------------|
| **`COOKIE_SECURE`** | **`true`** (HTTPS only) |
| **`COOKIE_SAMESITE`** | **`none`** (required so the browser includes cookies on cross-origin requests from your SPA) |
| **`CORS_ORIGINS`** | **`https://nudgeweb.app`** and, if you use it, **`https://www.nudgeweb.app`** (comma-separated, **no** trailing slash, **https** if the site is HTTPS) |
| **`REACT_APP_API_BASE_URL`** (SPA) | Full API origin, e.g. **`https://your-service.koyeb.app`** — same host the browser uses for `fetch`; no typo, no mixed `http`/`https` |

**Do not** set **`AUTH_COOKIE_DOMAIN`** to `nudgeweb.app` when the API lives on **`koyeb.app`** — that is invalid for cookie `Domain` and will break `Set-Cookie`.

**Sanity-check in DevTools (after `POST /auth/login` or register)**

1. **Application → Cookies → pick your API host** (`*.koyeb.app`): you should see **`access_token`** / **`refresh_token`**, **HttpOnly**, **Secure**, **SameSite=None**.
2. **Network → `GET /tasks/` (or any authed call) → Request headers**: there should be a **`Cookie:`** header on the request to the API. If it’s **missing**, the browser isn’t attaching cookies (SameSite / Secure / wrong API URL / third-party cookie rules).
3. **Network → login response → Response headers**: **`Set-Cookie`** should list those cookies with **`SameSite=None`** and **`Secure`**.

If cookies never stick or never send, a **same-origin proxy** (e.g. `https://nudgeweb.app/api/*` → Koyeb) avoids cross-site cookies entirely.

### Mobile (Safari / Chrome) — 401 on `/api/suggestions` while desktop works

Phones often enforce **stricter third-party / cross-site cookie rules** (e.g. Safari ITP). Your SPA may get **`Set-Cookie`** on login but **not** send **`Cookie`** on later `fetch` to the API host, so **`POST /api/suggestions`** returns **401**.

**Preferred fix:** route API through the **same site** as the SPA (reverse proxy) so cookies are **first-party**.

**Backend fallback:** **`POST /auth/login`**, **`POST /auth/register`**, and **`POST /auth/refresh`** always return **`access_token`** and **`refresh_token`** in JSON (alongside `Set-Cookie`) so the SPA can store **`nudge_access_token`** (or similar) and use **`Authorization: Bearer`** when cookies are not sent (e.g. Safari / ITP). The SPA should:

1. Store tokens in **memory** or **sessionStorage** (not localStorage if you can avoid it).
2. Send **`Authorization: Bearer <access_token>`** on API calls (middleware and `get_current_user` already accept Bearer).
3. On **401**, call **`POST /auth/refresh`** with JSON body **`{ "refresh_token": "<stored refresh>" }`** if the refresh **cookie** is missing (this endpoint accepts **cookie or body**).

**Security:** tokens in JS are **more exposed to XSS** than httpOnly cookies — use only if you need mobile web compatibility; tighten CSP and avoid inline scripts on the SPA.

---

## Flows (aligned with FE)

### Cold load

1. **`POST /auth/refresh`** — with cookies (`credentials: 'include'`) **or** JSON body `{ "refresh_token": "..." }` when the refresh cookie is missing (use the refresh from login/refresh JSON).
2. **401** → treat as **logged out** (no valid refresh).
3. **200** → new `Set-Cookie` for access + refresh; update stored tokens from the JSON body (`access_token`, `refresh_token`).

### Login

- **`POST /auth/login`** — `{ "password", "username" | "email" }`.
- Response **`Set-Cookie`**: access + refresh (names per env above).

### Register

- **`POST /auth/register`** — `{ "username", "email", "password" }`.
- **Password:** backend **`min_length=8`** (keep FE rule ≥ 8 in sync).
- Response **`Set-Cookie`** + user JSON (same shape as login user payload).

### Logout

- **`POST /auth/logout`** — clears cookies (server); FE clears non-auth UI cache after.

### Access expiry + axios 401 retry

1. On **401** from a **non-auth** route, FE may **`POST /auth/refresh` once** and **retry** the failed request.
2. If refresh returns **401**, user is **logged out** on the next action.

**Auth routes** (do not infinite-retry): e.g. `/auth/login`, `/auth/register`, `/auth/logout`, `/auth/refresh` — your interceptor should skip refresh-retry for these.

### Profile

- **`GET /auth/me`** — requires valid **access** JWT (cookie or Bearer).
- **200** body (map as needed):

```json
{
  "id": "<uuid>",
  "user_id": "<uuid>",
  "sub": "<uuid-string>",
  "username": "...",
  "user_name": "...",
  "email": "..."
}
```

- **401** — not authenticated or access expired (refresh then retry, or treat as logged out).
- **503** — `JWT_SECRET_KEY` not configured.

---

## Task & AI routes (JWT required)

Middleware + `Depends(get_current_user)` enforce auth on:

| Method | Path | Notes |
|--------|------|--------|
| `GET` | **`/tasks`**, **`/tasks/`** | Same handler; **current user’s tasks only**; JSON **array** of task objects with **`label`** (and `task_id`, `user_id`, etc.). |
| `POST` | **`/tasks/`** | Body **without `user_id`** — server sets `user_id` from JWT. Fields: `sentiment`, `category`, `label`, `context`, `time_of_day`, `amount_of_time`, `day_of_week`. |
| `POST` | `/api/tasks/enrich` | Body `{ "task", "taskHistory" }`; response includes **`task`** (enriched object) at `data.task`. |
| `POST` | `/api/suggestions` | Body `{ "taskHistory" }`; response **`suggestion`** with **`reccomendedTask`** and **`context`** (spelling matches existing UI). |

---

## JWT env (summary)

| Variable | Required | Description |
|----------|----------|-------------|
| `JWT_SECRET_KEY` | **Yes** (≥ **32** chars) | HS256 signing secret. |
| `JWT_ACCESS_EXPIRE_MINUTES` | No (default `15`) | Access TTL. |
| `JWT_REFRESH_EXPIRE_DAYS` | No (default `7`) | Refresh TTL. |
| `COOKIE_SECURE` | **Yes in prod** | `true` on HTTPS. |
| `COOKIE_SAMESITE` | No (`lax`) | `none` for cross-site API + SPA. |
| `AUTH_COOKIE_DOMAIN` | No | Optional e.g. `.example.com`. |
| `AUTH_ACCESS_COOKIE_NAME` | No | Override access cookie name. |
| `AUTH_REFRESH_COOKIE_NAME` | No | Override refresh cookie name. |

---

## Optional / legacy (still available)

- `GET /user_by_username/...`, `GET /user_by_id/...`, `POST /users/` — legacy helpers; main app flow should use **cookie auth** + **`/auth/*`** + **`/auth/me`**.

---

## Password storage

Passwords are hashed with the **`bcrypt`** package (directly; passwords over 72 UTF-8 bytes are SHA-256–prehashed first). **Magic link** is not implemented in this iteration.
