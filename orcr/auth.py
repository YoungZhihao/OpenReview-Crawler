"""Authentication.

Why this exists
---------------
Anonymous access to api2 is CAPTCHA-gated. After enough requests from one IP,
OpenReview answers ``/notes`` with a Cloudflare Turnstile challenge whose own
page says: *"Have an OpenReview account? Sign in to skip this check."* The
``/login`` API route is **not** behind that challenge, so a normal sign-in
yields a bearer token that clears it.

Security notes -- these are deliberate, not incidental:

* The password is used to obtain a token and then discarded. It is never
  written to disk, never passed on the command line (where it would land in
  shell history), and never logged.
* Only the resulting token is cached, in ``.openreview_token``.
* ``--login`` reads the password with :func:`getpass`, so it is not echoed and
  does not have to be shared with anyone -- including an AI assistant reading
  the terminal transcript.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_TOKEN_FILE = ".openreview_token"
TOKEN_ENV = "OPENREVIEW_TOKEN"
EMAIL_ENV = "OPENREVIEW_USER"
PASSWORD_ENV = "OPENREVIEW_PASSWORD"


class AuthError(RuntimeError):
    """Raised when sign-in fails."""


@dataclass
class Token:
    token: str
    user: str = ""
    obtained_at: float = 0.0
    expires_at: float | None = None

    @property
    def expires_in_hours(self) -> float | None:
        if not self.expires_at:
            return None
        return (self.expires_at - time.time()) / 3600

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= time.time() + 60)


# --------------------------------------------------------------------------- #
# JWT helpers (no signature verification -- we only read our own claims)
# --------------------------------------------------------------------------- #
def jwt_claims(token: str) -> dict:
    """Read the payload of a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (IndexError, ValueError, KeyError):
        return {}


def token_expiry(token: str) -> float | None:
    claims = jwt_claims(token)
    exp = claims.get("exp")
    try:
        return float(exp) if exp else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# sign in
# --------------------------------------------------------------------------- #
def login(base: str, email: str, password: str, timeout: float = 30.0) -> Token:
    """Exchange credentials for a bearer token.

    Returns a :class:`Token`; raises :class:`AuthError` with the server's own
    message on failure so a wrong password and a CAPTCHA read differently.
    """
    url = f"{base.rstrip('/')}/login"
    payload = json.dumps({"id": email, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "openreview-crawler/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = {}
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        message = body.get("message") or f"HTTP {exc.code}"
        raise AuthError(f"登录失败：{message}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AuthError(f"登录请求失败：{type(exc).__name__}: {exc}") from exc

    token = data.get("token") or data.get("accessToken")
    if not token:
        keys = ", ".join(sorted(data)) or "（空响应）"
        raise AuthError(
            "登录成功但响应里没有 token 字段。"
            f"实际返回字段：{keys}。"
            "如果这个账号启用了两步验证（MFA），请改用在浏览器里登录后复制 token 的方式。"
        )
    user = (data.get("user") or {}).get("id", "")
    return Token(
        token=token,
        user=user,
        obtained_at=time.time(),
        expires_at=token_expiry(token),
    )


def refresh(base: str, token: str, timeout: float = 30.0) -> Token | None:
    """Best-effort token refresh. Returns None when the endpoint refuses."""
    url = f"{base.rstrip('/')}/refreshToken"
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "openreview-crawler/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    new_token = data.get("token")
    if not new_token:
        return None
    return Token(token=new_token, user=(data.get("user") or {}).get("id", ""),
                 obtained_at=time.time(), expires_at=token_expiry(new_token))


# --------------------------------------------------------------------------- #
# token storage
# --------------------------------------------------------------------------- #
def load_token_file(path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE) -> str | None:
    """Read a cached token. Accepts either a JSON blob or a bare token string.

    Read as ``utf-8-sig`` because a byte-order mark left by an editor would
    otherwise become part of the token and every request would come back 401.
    """
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        text = file_path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    text = text.lstrip("\ufeff").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
            token = data.get("token")
            return token.strip() if isinstance(token, str) else None
        except ValueError:
            return None
    return text


def save_token_file(token: Token, path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE) -> Path:
    file_path = Path(path)
    file_path.write_text(
        json.dumps(
            {
                "token": token.token,
                "user": token.user,
                "obtained_at": token.obtained_at,
                "expires_at": token.expires_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:  # best effort: keep the secret out of other users' reach
        os.chmod(file_path, 0o600)
    except OSError:
        pass
    return file_path


def delete_token_file(path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE) -> bool:
    file_path = Path(path)
    if file_path.exists():
        file_path.unlink()
        return True
    return False


def env_credentials() -> tuple[str | None, str | None]:
    return os.environ.get(EMAIL_ENV), os.environ.get(PASSWORD_ENV)


def interactive_login(base: str, email: str | None = None,
                      password: str | None = None,
                      token_path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE,
                      out=print) -> Token:
    """Run a sign-in, prompting for whatever was not supplied.

    Intended for ``python crawl.py --login``: credentials are typed into the
    terminal by the account owner, so they never pass through a command line,
    a config file, or a chat transcript.
    """
    env_email, env_password = env_credentials()
    email = email or env_email
    password = password or env_password

    if not email:
        email = input("OpenReview 邮箱: ").strip()
    if not email:
        raise AuthError("没有提供邮箱。")
    if not password:
        password = getpass.getpass("OpenReview 密码（输入不会显示）: ")
    if not password:
        raise AuthError("没有提供密码。")

    out(f"正在以 {email} 登录 …")
    token = login(base, email, password)
    save_token_file(token, token_path)

    out(f"登录成功：{token.user or '(未知账号)'}")
    if token.expires_at:
        hours = token.expires_in_hours or 0
        out(f"令牌有效期约 {hours:.1f} 小时，已保存到 {token_path}")
    else:
        out(f"令牌已保存到 {token_path}")
    out("密码本身没有被保存，也没有写入任何文件。")
    return token


# --------------------------------------------------------------------------- #
# expiry: "use it while it lasts, ask when it runs out"
# --------------------------------------------------------------------------- #
# OpenReview issues a bearer token valid for exactly 24 hours. Its
# ``/refreshToken`` endpoint answers "No refresh token found" for a token
# obtained through ``/login`` -- the refresh token lives in a browser cookie --
# so there is no way to renew silently from here.
#
# Hence the contract this module implements: keep the token on disk, use it
# while it is valid, and when it has expired ask for the password once, in
# place, and carry on with whatever the user was already doing. The password is
# never stored -- re-typing it daily is the price of not keeping a secret at
# rest, which is the trade the user asked for.

def token_is_expired(token: str | None, margin: float = 120.0) -> bool:
    """True when the token is missing, or expires within ``margin`` seconds."""
    if not token:
        return True
    expiry = token_expiry(token)
    if expiry is None:
        # No exp claim: cannot tell locally, so let the API be the judge.
        return False
    return expiry <= time.time() + margin


def token_status(token: str | None) -> str:
    """A short human description of a token's state."""
    if not token:
        return "尚未登录"
    expiry = token_expiry(token)
    if not expiry:
        return "已登录（无法读取有效期）"
    remaining = expiry - time.time()
    stamp = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M")
    if remaining <= 0:
        return f"令牌已过期（{stamp}），需要重新登录"
    if remaining < 3600:
        return f"有效期剩余 {remaining / 60:.0f} 分钟（至 {stamp}）"
    return f"有效期剩余 {remaining / 3600:.1f} 小时（至 {stamp}）"


def remembered_email(token_path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE,
                     explicit: str | None = None) -> str:
    """The email to prefill in a login prompt.

    Order: explicit argument, ``OPENREVIEW_USER``, then whoever the last saved
    token belonged to -- so the address only has to be typed once, ever.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get(EMAIL_ENV)
    if env:
        return env.strip()
    file_path = Path(token_path)
    if file_path.exists():
        try:
            data = json.loads(file_path.read_text(encoding="utf-8-sig").lstrip("\ufeff"))
            user = data.get("user")
            if isinstance(user, str) and user.strip():
                return user.strip()
        except (OSError, ValueError):
            pass
    return ""


# A prompter takes the email to prefill and returns (email, password), or None
# to cancel. The CLI uses the console one; the GUI supplies its own.
Prompter = "Callable[[str], tuple[str, str] | None]"


def console_prompter(email_default: str = "") -> tuple[str, str] | None:
    """Ask for credentials on a terminal; the password is not echoed."""
    suffix = f" [{email_default}]" if email_default else ""
    try:
        email = input(f"OpenReview 邮箱{suffix}: ").strip() or email_default
    except (EOFError, KeyboardInterrupt):
        return None
    if not email:
        return None
    try:
        password = getpass.getpass("OpenReview 密码（输入不会显示）: ")
    except (EOFError, KeyboardInterrupt):
        return None
    if not password:
        return None
    return email, password


def ensure_token(
    base: str,
    token_path: str | os.PathLike[str] = DEFAULT_TOKEN_FILE,
    token: str | None = None,
    email: str | None = None,
    prompter=None,
    log=print,
    force: bool = False,
    margin: float = 120.0,
) -> str | None:
    """Return a usable bearer token, signing in first if it has expired.

    Returns ``None`` when the user declined to sign in, so callers can stop
    cleanly instead of crashing. Raises :class:`AuthError` when a sign-in was
    attempted and failed.
    """
    if not force and not token_is_expired(token, margin=margin):
        return token

    saved_email = remembered_email(token_path, email)
    if token and not force:
        log(f"令牌已过期（{token_status(token)}），需要重新登录。")

    prompter = prompter or console_prompter
    credentials = prompter(saved_email)
    if not credentials:
        log("已取消登录。")
        return None
    email_value, password = credentials

    log(f"正在以 {email_value} 登录 …")
    fresh = login(base, email_value, password)
    save_token_file(fresh, token_path)
    log(f"登录成功：{fresh.user or email_value} · {token_status(fresh.token)}")
    return fresh.token

