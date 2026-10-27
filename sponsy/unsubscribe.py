"""Unsubscribe: the header Gmail turns into its own "Unsubscribe" link, and
the signed one-click URL behind it.

Why it matters even for one-to-one outreach: a recipient who does not want
the email has two buttons, "Unsubscribe" and "Report spam". The first costs
nothing; the second is the spam rate Gmail measures you on (stay under 0.1%,
never reach 0.3%). Making the polite exit the easy one is the cheapest
reputation protection there is.

  List-Unsubscribe: <https://PUBLIC_URL/u/P01/sig>, <mailto:you?subject=unsubscribe>
  List-Unsubscribe-Post: List-Unsubscribe=One-Click         (RFC 8058)

The https form only appears when PUBLIC_URL is set (the web server must be
reachable there); RFC 8058 one-click needs it. The mailto form is always
there: Gmail sends that email from the recipient's account, the reply poll
sees "unsubscribe" in the subject, and the existing stop rule declines the
prospect and blocks the address without asking the model.

The link is signed with a secret kept in data/, so nobody can unsubscribe
someone else by guessing ids.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

PID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def secret(cfg) -> bytes:
    """SPONSY_SECRET if set, else a random key created once in data/."""
    value = (getattr(cfg, "secret", "") or "").strip()
    if value:
        return value.encode("utf-8")
    path = Path(getattr(cfg, "db_path", "data/sponsy.db")).parent / "secret.key"
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_hex(32).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return data


def signature(cfg, pid: str) -> str:
    mac = hmac.new(secret(cfg), "unsubscribe:{}".format(pid).encode("utf-8"),
                   hashlib.sha256)
    return mac.hexdigest()[:32]


def verify(cfg, pid: str, sig: str) -> bool:
    if not PID.match(pid or "") or not re.fullmatch(r"[0-9a-f]{32}", sig or ""):
        return False
    return hmac.compare_digest(signature(cfg, pid), sig)


def url_for(cfg, pid: str) -> str:
    base = (getattr(cfg, "public_url", "") or "").strip().rstrip("/")
    if not base or not PID.match(pid or ""):
        return ""
    return "{}/u/{}/{}".format(base, pid, signature(cfg, pid))


def headers(cfg, pid: str, mailbox: str) -> dict[str, str]:
    """The two headers for one recipient. `mailbox` is the address the reply
    poll reads -- the sending account, not Reply-To, which may be an inbox
    this bot never sees."""
    parts = []
    link = url_for(cfg, pid)
    if link:
        parts.append("<{}>".format(link))
    if mailbox and "@" in mailbox:
        parts.append("<mailto:{}?subject=unsubscribe>".format(quote(mailbox, safe="@")))
    out = {}
    if parts:
        out["List-Unsubscribe"] = ", ".join(parts)
    if link:
        out["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return out


# ---------------------------------------------------------------------------
# what the link does
# ---------------------------------------------------------------------------

def apply(db, notifier, pid: str, how: str = "one-click link") -> str:
    """Decline the prospect and block the address. Idempotent: a second
    click, or a mail provider's retry, finds it already done."""
    from . import store  # noqa: PLC0415

    prospect = store.get(db, pid)
    if prospect is None:
        return "unknown"
    if prospect.status == "declined" and store.is_blocked(db, prospect.email):
        return "already"
    store.mark_never(db, pid, reason="unsubscribed via " + how)
    if notifier is not None:
        try:
            from .telegram import esc  # noqa: PLC0415
            notifier.send("<b>{}</b> ({}) unsubscribed via the {}. Declined and "
                          "blocked; nothing more will be sent.".format(
                              esc(prospect.company), esc(pid), esc(how)))
        except Exception as exc:                   # noqa: BLE001
            print("[unsubscribe] could not notify: {}".format(exc))
    return "done"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;padding:48px 16px;
background:#f6f6f8;color:#222}}main{{max-width:460px;margin:auto;background:#fff;
border:1px solid #e3e3e8;border-radius:10px;padding:28px}}h1{{font-size:20px;
margin:0 0 12px}}button{{font:inherit;padding:10px 18px;border-radius:8px;
border:0;background:#6c47c7;color:#fff;cursor:pointer}}
@media (prefers-color-scheme:dark){{body{{background:#16161a;color:#eee}}
main{{background:#202026;border-color:#33333b}}}}</style></head>
<body><main><h1>{title}</h1>{body}</main></body></html>"""


def page(title: str, body_html: str) -> bytes:
    import html  # noqa: PLC0415
    return PAGE.format(title=html.escape(title), body=body_html).encode("utf-8")
