"""Find a published contact address for a prospect, with the page it came from.

The rule that governs the rest of this project applies here too: an address is
only usable if we can point at where it was published. Nothing is guessed, and
nothing is written to the prospect list without a human seeing the source.

Most dev-tool companies publish a sponsorships@ or devrel@ address on a
community or sponsorship page. Some only offer a form -- that is a useful
answer too, and it gets reported rather than papered over.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from .research import fetch_url, search

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# local-parts that mean "a human who handles partnerships will read this"
GOOD = {"sponsor", "sponsors", "sponsorship", "sponsorships", "partner",
        "partners", "partnership", "partnerships", "devrel", "community",
        "events", "developerrelations", "developer", "developers", "campus",
        "students", "university", "hackathon", "hackathons", "education",
        "outreach", "ecosystem", "csr"}
OKAY = {"hello", "hi", "contact", "team", "info", "connect", "enquiries",
        "inquiries", "marketing", "brand", "branding"}
# local-parts that are a dead end or actively wrong to write to
BAD = {"noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "legal",
       "abuse", "dmca", "security", "secuty", "postmaster", "webmaster",
       "unsubscribe", "press", "pr", "media", "investors", "investor", "ir",
       "billing", "invoices", "accounts", "careers", "jobs", "recruiting",
       "hr", "sales", "support", "help", "care", "feedback", "complaints",
       "sentry", "wordpress", "example", "email", "name", "user", "test",
       "website"}
ASSET_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}
# pages that republish addresses without the owner's involvement
UNTRUSTED_SOURCES = ("pissedconsumer", "complaint", "scamadviser", "trustpilot",
                     "leadiq", "rocketreach", "zoominfo", "apollo.io",
                     "signalhire", "contactout", "hunter.io", "lusha",
                     "cufinder", "aeroleads")
JUNK_DOMAINS = {"example.com", "example.org", "sentry.io", "wixpress.com",
                "schema.org", "w3.org", "googlemail.com", "email.com",
                "domain.com", "yourcompany.com", "sentry-next.wixpress.com"}
FREE_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com",
                "outlook.com", "live.com", "icloud.com", "protonmail.com",
                "proton.me", "rediffmail.com", "ymail.com"}
# "zerodha.co.in" -> "zerodha": the label before one of these is the company
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "edu", "gov", "or", "ne"}

QUERIES = (
    "{company} sponsorship contact email hackathon",
    "{company} developer relations community contact email",
)
SPONSORSHIP_PATH_WORDS = ("sponsor", "partner", "community", "developer-program",
                          "student", "campus", "education")

# "sponsorships [at] acme [dot] io" is still a published address
OBFUSCATED_AT = re.compile(r"\s*[\[({]\s*at\s*[\])}]\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*[\[({]\s*dot\s*[\])}]\s*", re.I)


@dataclass
class Candidate:
    email: str
    source_url: str
    score: int = 0
    note: str = ""


@dataclass
class Finding:
    prospect_id: str
    company: str
    candidates: list[Candidate] = field(default_factory=list)
    pages_seen: list[str] = field(default_factory=list)
    form_urls: list[str] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


# ---------------------------------------------------------------------------
# domains
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _host(url: str) -> str:
    """Host of a URL or bare domain, without www. Blank when unparseable."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def _domain_label(host: str) -> str:
    """The label that names the company: 'community.vercel.com' -> 'vercel',
    'zerodha.co.in' -> 'zerodha', 'polygon.technology' -> 'polygon'."""
    labels = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        labels = labels[:-2]
    elif len(labels) >= 2:
        labels = labels[:-1]
    return labels[-1] if labels else ""


def _company_slugs(company: str, website: str = "") -> set[str]:
    """What the company's own domain may look like: its name, and the host of
    the website we already know for it. 'Meta Platforms' at fb.com is only
    matchable through the second."""
    out: set[str] = set()
    slug = _slug(company)
    if slug:
        out.add(slug)
    label = _slug(_domain_label(_host(website)))
    if label:
        out.add(label)
    return out


# A domain that is the company name plus one of these is still plausibly the
# same organisation. Anything else appended -- "care", "tech", "science" --
# is usually a different company that happens to start the same way.
SAFE_DOMAIN_SUFFIXES = ("inc", "corp", "co", "group", "hq", "global",
                        "official", "india", "team")


def _same_company(label: str, slugs: set[str]) -> bool:
    """Is this domain plausibly theirs?

    The direction matters. A domain SHORTER than the company name is a
    shortening -- acme.io for "Acme Analytics" -- and is normal. A domain
    LONGER than the company name is usually somebody else who happens to
    start the same way, which is how a live sweep offered miracare.com (a
    fertility tracker) for the hackathon sponsor "Mira" and avadhtech.com (an
    engineering college) for "Avadh". Both passed a plain containment test.
    """
    label = _slug(label)
    if not label:
        return False
    for slug in slugs:
        if not slug:
            continue
        if label == slug:
            return True
        if len(label) >= 4 and label in slug:          # domain is a shortening
            return True
        if len(slug) >= 4 and label.startswith(slug) \
                and label[len(slug):] in SAFE_DOMAIN_SUFFIXES:
            return True
    return False


def _on_their_site(url: str, slugs: set[str]) -> bool:
    return _same_company(_domain_label(_host(url)), slugs)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_email(address: str, company: str, source_url: str,
                website: str = "") -> tuple[int, str]:
    """Accept only role mailboxes on the company's own domain.

    Learned the hard way from a live sweep of 20 companies: loose scoring
    surfaced another charity's sponsorship address for Groq, two employees'
    personal addresses scraped off a Devpost page for Polygon, and a support
    address lifted from a complaints site for Zerodha. One usable result out of
    twenty is not worth three ways to embarrass yourself, so the filter fails
    closed: wrong domain, or not a role mailbox, means no result at all.

    `website`, when the prospect list already has one, is a second spelling
    of "their own domain" -- the company name alone cannot tell that
    community@fb.com belongs to Meta.
    """
    address = (address or "").strip().strip(".,;:<>()[]\"'").lower()
    local, _, domain = address.partition("@")
    domain = domain.rstrip(".")
    if not local or not domain or "." not in domain:
        return -99, "not an email address"
    base = re.split(r"[.\-_+]", local)[0]

    if domain in JUNK_DOMAINS or domain.rsplit(".", 1)[-1] in ASSET_EXTENSIONS:
        return -99, "placeholder or asset domain"
    if base in BAD or local in BAD:
        return -99, "not a mailbox for outreach"
    if len(local) > 40 or local.count(".") > 3:
        return -99, "looks machine-generated"
    if domain in FREE_DOMAINS:
        return -99, "free-mail domain"

    # 1. it must be their domain. A partnership mailbox at some other
    #    organisation is worse than nothing.
    slugs = _company_slugs(company, website)
    if not slugs or not _same_company(_domain_label(domain), slugs):
        return -99, "not the company's own domain"

    # 2. it must be a role mailbox. `sandipan@` is a person who did not ask to
    #    be cold-emailed by a script.
    if base in GOOD or local in GOOD:
        score, notes = 5, ["partnership mailbox"]
    elif base in OKAY or local in OKAY:
        score, notes = 3, ["general mailbox"]
    else:
        return -99, "looks like a personal address, not a role mailbox"

    # 3. it must come from somewhere the owner put it. A scraper site's copy
    #    of a real address is still a scraper site's word for it.
    if any(bad in (source_url or "").lower() for bad in UNTRUSTED_SOURCES):
        return -99, "source is a scraper/complaints site"

    notes.append("company domain")
    if _on_their_site(source_url, slugs):
        score += 1
        notes.append("published on their own site")
    else:
        score -= 1
        notes.append("found on a third-party page")
    return score, ", ".join(notes)


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def _addresses(text: str) -> set[str]:
    text = OBFUSCATED_DOT.sub(".", OBFUSCATED_AT.sub("@", text or ""))
    return {raw.strip(".,;:").lower() for raw in EMAIL.findall(text)}


def _looks_like_sponsorship_page(url: str, text: str) -> bool:
    low_url = url.lower()
    return (any(w in low_url for w in SPONSORSHIP_PATH_WORDS)
            or text.lower().count("sponsor") >= 3)


def find_for(cfg, prospect, max_pages: int = 3) -> Finding:
    """Search for a published address, read up to `max_pages` pages, and keep
    only what score_email accepts. Also collects sponsorship pages on their
    own site: where to apply matters as much as who to write to, since most
    dev-tool companies route sponsorship through a form, not an inbox."""
    finding = Finding(prospect_id=prospect.id, company=prospect.company)
    website = (getattr(prospect, "website", "") or "").strip()
    slugs = _company_slugs(prospect.company, website)
    seen: dict[str, Candidate] = {}
    urls: list[str] = []

    for template in QUERIES:
        try:
            results = search(cfg, template.format(company=prospect.company), k=4)
        except Exception:
            results = []
        for result in results:
            url = result.get("url", "")
            if url and url not in urls:
                urls.append(url)
        if results:
            break
    if website:
        # The one page we know is theirs. Last, because targeted search hits
        # are better; still worth a look when search is off or came up empty.
        url = website if "://" in website else "https://" + website
        if url not in urls:
            urls.append(url)

    for url in urls[:max_pages]:
        try:
            _, text = fetch_url(cfg, url)
        except Exception:
            continue
        finding.pages_seen.append(url)
        if _on_their_site(url, slugs) and _looks_like_sponsorship_page(url, text):
            finding.form_urls.append(url)
        for address in sorted(_addresses(text)):
            if address in seen:
                continue
            score, note = score_email(address, prospect.company, url, website=website)
            if score <= 0:
                continue
            seen[address] = Candidate(email=address, source_url=url, score=score,
                                      note=note)

    finding.candidates = sorted(seen.values(), key=lambda c: (-c.score, c.email))
    return finding
