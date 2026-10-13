"""Optional password protection for the whole app (set APP_PASSWORD in .env).

A signed cookie (HMAC over an expiry time, key stored in data/) says you've signed in. Wrong passwords are throttled. Requests
that arrive through `tailscale serve` look like they come from localhost, so source IP is never used to skip the check.
"""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config

COOKIE = "jt_session"
MAX_AGE = 30 * 24 * 3600
MAX_FAILS, WINDOW, LOCKOUT = 5, 300, 300
_fails: list[float] = []
_locked_until = 0.0


def _key() -> bytes:
    path = config.DATA_DIR / "session.key"
    if not path.exists():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def make_token() -> str:
    exp = str(int(time.time()) + MAX_AGE)
    return f"{exp}.{hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest())


def password_ok(candidate: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(config.APP_PASSWORD.encode()).digest())


def lockout_remaining() -> int:
    return max(0, int(_locked_until - time.time()))


def record_failure():
    global _locked_until
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < WINDOW] + [now]
    if len(_fails) >= MAX_FAILS:
        _locked_until = now + LOCKOUT
        _fails.clear()


def reset_throttle():
    global _locked_until
    _fails.clear()
    _locked_until = 0.0


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def set_cookie(response, request: Request):
    response.set_cookie(COOKIE, make_token(), max_age=MAX_AGE, httponly=True, samesite="lax", secure=_secure(request))


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title><style>
:root{color-scheme:light dark;--bg:#f6f6f3;--surface:#fff;--text:#1b1b19;--muted:#6c6c66;--line:#e3e3dd;--accent:#2f5bea}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--surface:#1c1c1a;--text:#ecece8;--muted:#9b9b93;--line:#2d2d2a;--accent:#7b97ff}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
form{width:min(360px,90vw);background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:26px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:var(--muted);font-size:13px}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line);background:var(--surface);color:var(--text);font:inherit}
button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:9px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6}#err{color:#b91c1c;font-size:13px;min-height:20px;margin-top:10px}
</style></head><body><form id="f"><h1>Job Tracker</h1><p>Enter your password to continue.</p>
<input id="pw" type="password" autocomplete="current-password" autofocus placeholder="Password" required>
<button id="go">Sign in</button><div id="err"></div></form>
<script>
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault(); const b = document.getElementById("go"), err = document.getElementById("err"); b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: document.getElementById("pw").value})});
    if (r.ok) { location.href = "/"; return; }
    err.textContent = (await r.json().catch(() => ({}))).detail || "Couldn't sign in";
  } catch { err.textContent = "Couldn't reach the server"; }
  b.disabled = false;
});
</script></body></html>"""


async def middleware(request: Request, call_next):
    if not config.APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    # state-changing requests must come from this site (cookies are SameSite=Lax; this is a second check)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    if path in ("/login", "/logout"):
        return await call_next(request)
    if valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_PAGE)
