"""DNS lookups over HTTPS, for the two questions deliverability asks of DNS.

  1. Can this recipient's domain receive mail at all? (MX, before we send)
     A hard bounce is the cheapest reputation damage there is to avoid: one
     lookup, cached for a week, instead of a 550 that Gmail counts against
     the sender.
  2. Is OUR domain set up so receivers can trust it? (SPF, DKIM, DMARC, for
     `doctor`). Gmail and Outlook junk or reject mail that fails these.

The standard library cannot ask for an MX or TXT record, and a resolver
package is one more dependency for two record types, so this asks a public
DNS-over-HTTPS service with `requests`, which is already a dependency.
Nothing here ever probes a mail server: SMTP "verification" (RCPT TO and hang
up) is itself a pattern receivers flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DOH_SERVERS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")
TYPES = {"A": 1, "MX": 15, "TXT": 16, "AAAA": 28}
CACHE_JOB = "dns"
OK_TTL = timedelta(days=7)
BAD_TTL = timedelta(days=1)


@dataclass
class Answer:
    status: str                    # ok | nxdomain | nodata | error
    records: list[str] = field(default_factory=list)
    error: str = ""


def query(name: str, rtype: str, timeout: float = 6.0) -> Answer:
    """One record type for one name. Never raises: a network problem is
    `error`, which every caller treats as "don't know", never as "bad"."""
    import requests  # noqa: PLC0415

    last = ""
    for server in DOH_SERVERS:
        try:
            resp = requests.get(server, params={"name": name, "type": rtype},
                                headers={"Accept": "application/dns-json"},
                                timeout=timeout)
            if resp.status_code != 200:
                last = "HTTP {} from {}".format(resp.status_code, server)
                continue
            data = resp.json()
        except Exception as exc:                   # noqa: BLE001
            last = "{}: {}".format(type(exc).__name__, exc)
            continue
        code = data.get("Status")
        if code == 3:
            return Answer("nxdomain")
        if code != 0:
            last = "DNS status {} from {}".format(code, server)
            continue
        want = TYPES.get(rtype)
        records = [str(a.get("data", "")) for a in data.get("Answer") or []
                   if a.get("type") == want]
        if rtype == "TXT":
            # TXT strings arrive quoted and sometimes split into 255-byte
            # chunks: '"v=DKIM1; k=rsa; " "p=MIIB..."'.
            records = ["".join(part for part in _txt_parts(r)) for r in records]
        return Answer("ok" if records else "nodata", records)
    return Answer("error", error=last)


def _txt_parts(record: str) -> list[str]:
    record = record.strip()
    if not record.startswith('"'):
        return [record]
    parts, buf, quoted, escape = [], "", False, False
    for ch in record:
        if escape:
            buf += ch
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            if quoted:
                parts.append(buf)
                buf = ""
            quoted = not quoted
        elif quoted:
            buf += ch
    return parts


def domain_of(address: str) -> str:
    return (address or "").strip().lower().rpartition("@")[2].rstrip(".")


# ---------------------------------------------------------------------------
# 1. can the recipient receive mail?
# ---------------------------------------------------------------------------

def mail_domain_status(domain: str, lookup=query) -> tuple[str, str]:
    """('ok' | 'none' | 'unknown', why).

    'none' only on positive evidence: the domain does not exist, publishes a
    null MX (RFC 7505: "this domain takes no mail"), or has neither MX nor an
    address record to fall back to. Any lookup failure is 'unknown', and
    unknown sends: our network having a bad minute must never block a
    legitimate email.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return "none", "no domain"
    mx = lookup(domain, "MX")
    if mx.status == "nxdomain":
        return "none", "{} does not exist".format(domain)
    if mx.status == "error":
        return "unknown", mx.error
    if mx.status == "ok":
        hosts = [r.split()[-1].rstrip(".") if r.split() else "" for r in mx.records]
        if all(h in ("", ".") for h in hosts):
            return "none", "{} publishes a null MX: it accepts no mail".format(domain)
        return "ok", "MX " + ", ".join(h for h in hosts if h)[:120]
    # No MX: RFC 5321 falls back to the A/AAAA record.
    for rtype in ("A", "AAAA"):
        addr = lookup(domain, rtype)
        if addr.status == "ok":
            return "ok", "no MX, delivers to its {} record".format(rtype)
        if addr.status == "error":
            return "unknown", addr.error
    return "none", "{} has no MX and no address record".format(domain)


def cached_mail_domain_status(db, domain: str, lookup=query,
                              now: datetime | None = None) -> tuple[str, str]:
    """mail_domain_status, remembered in the memory table: a week for a
    good answer, a day for a bad one, never for 'unknown'."""
    now = now or datetime.now(timezone.utc)
    key = (domain or "").strip().lower()
    raw = db.memory_get(CACHE_JOB, key) if key else None
    if raw:
        try:
            hit = json.loads(raw)
            at = datetime.fromisoformat(hit["at"])
            ttl = OK_TTL if hit["status"] == "ok" else BAD_TTL
            if now - at < ttl:
                return hit["status"], hit.get("why", "")
        except (ValueError, KeyError, TypeError):
            pass
    status, why = mail_domain_status(key, lookup=lookup)
    if status != "unknown" and key:
        db.memory_set(CACHE_JOB, key, json.dumps(
            {"status": status, "why": why, "at": now.isoformat(timespec="seconds")}))
    return status, why


# ---------------------------------------------------------------------------
# 2. is our own domain trustworthy?
# ---------------------------------------------------------------------------

# Domains whose authentication is the mailbox provider's job, not yours.
PROVIDER_DOMAINS = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                    "live.com", "yahoo.com", "icloud.com", "proton.me",
                    "protonmail.com", "zoho.com")


@dataclass
class AuthReport:
    domain: str
    spf: str = ""                  # the v=spf1 record, "" if none
    spf_problem: str = ""
    dkim: str = ""                 # selector that answered, "" if none found
    dkim_bits: int = 0
    dmarc: str = ""                # the v=DMARC1 record, "" if none
    dmarc_policy: str = ""
    mx: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """What Gmail requires of every sender: SPF or DKIM."""
        return bool((self.spf and not self.spf_problem) or self.dkim)

    def problems(self) -> list[str]:
        out = []
        if not self.spf:
            out.append("no SPF record")
        elif self.spf_problem:
            out.append("SPF: " + self.spf_problem)
        if not self.dkim:
            out.append("no DKIM key found")
        elif self.dkim_bits and self.dkim_bits < 1024:
            out.append("DKIM key is ~{} bits; Gmail wants 1024+, 2048 is better"
                       .format(self.dkim_bits))
        if not self.dmarc:
            out.append("no DMARC record")
        if not self.mx:
            out.append("no MX record, so replies and bounces cannot come back")
        return out


def _dkim_bits(record: str) -> int:
    """Rough RSA size from the base64 public key length (good to ~100 bits)."""
    key = ""
    for part in record.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == "p":
            key = value.strip()
    if not key:
        return 0
    der_bytes = len(key) * 3 // 4
    return int(round((der_bytes - 38) * 8 / 256.0)) * 256 if der_bytes > 38 else 0


def check_sender_domain(domain: str, selectors: list[str], expect_include: str = "",
                        lookup=query) -> AuthReport:
    """SPF, DKIM (each selector tried in turn), DMARC and MX for `domain`."""
    report = AuthReport(domain=domain)
    txt = lookup(domain, "TXT")
    if txt.status == "error":
        report.errors.append("TXT lookup failed: " + txt.error)
    spf = [r for r in txt.records if r.lower().startswith("v=spf1")]
    if len(spf) > 1:
        report.spf, report.spf_problem = spf[0], "{} SPF records; receivers treat " \
            "that as no SPF at all, merge them into one".format(len(spf))
    elif spf:
        report.spf = spf[0]
        terms = spf[0].lower().split()
        if "+all" in terms or "all" in terms:        # a bare "all" means +all
            report.spf_problem = "ends in +all, which lets anyone send as you"
        elif expect_include and "include:" + expect_include not in terms \
                and "redirect=" + expect_include not in terms:
            report.spf_problem = "does not include {} (your mail provider)".format(
                expect_include)

    for selector in selectors:
        if not selector:
            continue
        rec = lookup("{}._domainkey.{}".format(selector, domain), "TXT")
        # A key record has a non-empty p= tag (v=DKIM1 is optional, and an
        # empty p= is a revoked key).
        hit = [r for r in rec.records if re.search(r"(^|;)\s*p=[^;\s]", r)]
        if hit:
            report.dkim = selector
            report.dkim_bits = _dkim_bits(hit[0])
            break

    dmarc = lookup("_dmarc." + domain, "TXT")
    found = [r for r in dmarc.records if r.lower().startswith("v=dmarc1")]
    if found:
        report.dmarc = found[0]
        for part in found[0].split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip().lower() == "p":
                report.dmarc_policy = value.strip().lower()

    mx = lookup(domain, "MX")
    report.mx = mx.status == "ok"
    if mx.status == "error":
        report.errors.append("MX lookup failed: " + mx.error)
    return report
