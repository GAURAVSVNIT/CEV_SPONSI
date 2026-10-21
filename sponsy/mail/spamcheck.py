"""Content that makes a filter suspicious, flagged on the card before you tap.

Authentication and reputation decide most of where an email lands, but the
words still count at the margin -- and they are the one part a human can fix
in ten seconds. This is a warning list, never a gate: the drafting prompt
already forbids most of it, and a human may have a good reason.

Kept deliberately short. Long "spam word" lists flag ordinary English ("free
licences", "offer") and teach people to ignore the warning.
"""

from __future__ import annotations

import re

SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly",
              "rebrand.ly", "cutt.ly", "shorturl.at", "rb.gy", "tiny.cc", "lnkd.in")
URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
IP_URL = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}", re.I)
SPAMMY = re.compile(
    r"\b(act now|limited time|urgent(?:ly)?|100% free|risk[- ]free|guaranteed?|"
    r"click here|no obligation|winner|congratulations|cash bonus|earn money|"
    r"once in a lifetime|exclusive deal|don'?t miss out|order now|buy now|"
    r"special promotion|lowest price|double your)\b", re.I)
FAKE_THREAD = re.compile(r"^\s*(re|fw|fwd)\s*:", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅]")
MAX_LINKS = 3
MAX_SUBJECT = 78


def _caps_words(text: str) -> list[str]:
    return [w for w in re.findall(r"\b[A-Z]{4,}\b", text or "")
            if w not in ("ACM", "SVNIT", "NIT", "IIT", "API", "HTML", "AI", "USD", "INR")]


def check(subject: str, body: str, kind: str = "initial",
          threaded: bool = False) -> list[str]:
    """Warnings, most important first. `threaded` says the email really is a
    reply in an existing thread, which is the only time Re: is honest."""
    out: list[str] = []
    subject, body = subject or "", body or ""

    if FAKE_THREAD.match(subject) and not threaded:
        out.append("subject starts with Re:/Fwd: but this is not a reply; Gmail "
                   "treats fake threads as a spam signal")
    if len(subject) > MAX_SUBJECT:
        out.append("subject is {} characters; under {} reads better and is not "
                   "cut off".format(len(subject), MAX_SUBJECT))
    if "!" in subject:
        out.append("exclamation mark in the subject")
    if EMOJI.search(subject):
        out.append("emoji in the subject")
    caps = _caps_words(subject)
    if caps:
        out.append("ALL-CAPS in the subject: " + ", ".join(caps[:3]))

    links = URL.findall(body)
    short = [u for u in links if any(
        re.match(r"https?://(www\.)?" + re.escape(s) + r"(/|$)", u, re.I)
        for s in SHORTENERS)]
    if short:
        out.append("link shortener ({}); filters assume it hides the real "
                   "destination, use the full URL".format(short[0]))
    if IP_URL.search(body):
        out.append("link to a bare IP address")
    if len(links) > MAX_LINKS:
        out.append("{} links in the body; keep a cold email to {} or fewer"
                   .format(len(links), MAX_LINKS))
    http = [u for u in links if u.lower().startswith("http://")]
    if http:
        out.append("unencrypted http:// link ({})".format(http[0][:60]))

    phrases = sorted({m.group(0).lower() for m in SPAMMY.finditer(subject + "\n" + body)})
    if phrases:
        out.append("salesy phrasing: " + ", ".join(phrases[:4]))
    if body.count("!") >= 3:
        out.append("{} exclamation marks in the body".format(body.count("!")))
    caps = _caps_words(body)
    if len(caps) >= 3:
        out.append("ALL-CAPS words in the body: " + ", ".join(caps[:3]))
    if re.search(r"[$₹€£]\s?\d", subject):
        out.append("a price in the subject")
    return out
