"""Gmail SMTP with an app password: the five-minute path.

STARTTLS on 587 (or implicit TLS on 465), one login, one message. Never
plaintext: Gmail's sender rules require TLS, and so does an app password. Replies then have to be read over
IMAP with the same password, which is why gmail_api is the preferred channel
once someone has ten minutes for the OAuth flow.
"""

from __future__ import annotations

from email.message import EmailMessage


class SmtpNotConfigured(RuntimeError):
    pass


def _require(cfg) -> None:
    """Fail with the names of the missing .env keys instead of a socket error
    from inside smtplib, because this is what `doctor` shows the user."""
    missing = [name for name, value in (("SMTP_HOST", cfg.smtp_host),
                                        ("SMTP_USER", cfg.smtp_user),
                                        ("SMTP_PASSWORD", cfg.smtp_password))
               if not value]
    if missing:
        raise SmtpNotConfigured("SMTP is not configured: set {} in .env "
                                "(Gmail: an app password, not your login password)"
                                .format(", ".join(missing)))


# A brochure plus embedded logos is a few megabytes, and base64 adds a third
# on top. Thirty seconds is fine for a handshake and nowhere near enough to
# push 5 MB up a home connection, which shows up as SMTPServerDisconnected
# half way through the upload.
HANDSHAKE_TIMEOUT = 30
UPLOAD_TIMEOUT = 300


def _connect(cfg, timeout: int = HANDSHAKE_TIMEOUT):
    import smtplib  # noqa: PLC0415

    if int(cfg.smtp_port) == 465:
        # Implicit TLS (SMTPS). Some hosts and some networks only offer this.
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
        server.ehlo()
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
        server.ehlo()
        server.starttls()                # smtplib re-EHLOs before login
    server.login(cfg.smtp_user, cfg.smtp_password)
    return server


def send_smtp(cfg, msg: EmailMessage) -> str:
    """Send one message; returns its Message-ID (the value we store).

    Retried once: Gmail drops a connection now and then, and the caller has
    already recorded a human's approval, so one more attempt is cheaper than
    making them tap again.
    """
    _require(cfg)
    last: Exception | None = None
    for attempt in range(2):
        try:
            with _connect(cfg, timeout=UPLOAD_TIMEOUT) as server:
                server.send_message(msg)
            return msg["Message-ID"]
        except Exception as exc:         # noqa: BLE001 -- reported by the caller
            last = exc
            if attempt == 0:
                print("[mail] smtp attempt 1 failed ({}: {}); retrying"
                      .format(type(exc).__name__, exc))
    raise last


def check_login(cfg) -> None:
    """The same handshake as a send, without a message. Used by `doctor`:
    a wrong app password shows up here, not half way through an approval."""
    _require(cfg)
    with _connect(cfg):
        pass
