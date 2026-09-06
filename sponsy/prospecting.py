"""Find prospects by looking at who already sponsors events like yours.

A prospect list typed from memory is exactly the kind of ungrounded guess the
rest of this project refuses to make. This does it properly: search for the
sponsor pages of comparable events, fetch them, have the model list the
companies actually presented there, and rank by how many distinct pages each
one turns up on.

A company on four student-event sponsor pages has demonstrated that it
sponsors student events. That is evidence. "Feels like a good fit" is not.
Nothing else is inferred: a Lead carries the pages it was seen on and no
invented website, so the next step (contacts.find_for) starts from a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import prompts
from . import research as research_mod
from .agent import structured
from .llm import QuotaExhausted
from .research import fetch_url, search
from .store import find_by_company, norm_company

# Platforms, hosts and channels an extractor mistakes for sponsors. The host
# club's own words are added per event (see host_words).
NOT_A_SPONSOR = {
    "devpost", "devfolio", "unstop", "mlh", "major league hacking", "github",
    "hackerearth", "dorahacks", "eventbrite", "linkedin", "instagram",
    "twitter", "x", "facebook", "youtube", "medium", "notion", "google forms",
    "discord", "whatsapp", "telegram",
}

# Words in a club name that say nothing about which club it is. "ACM SVNIT"
# should ban "acm" and "svnit"; "The Coding Club" should ban nothing useful.
HOST_STOPWORDS = {"the", "and", "of", "for", "club", "clubs", "society", "team",
                  "student", "students", "chapter", "cell", "committee",
                  "council", "association", "college", "university", "institute"}

# Tier labels and page furniture that come back as "sponsor names".
LABELS = {"sponsors", "sponsor", "partners", "partner", "our sponsors",
          "our partners", "title sponsor", "title sponsors", "gold", "silver",
          "bronze", "platinum", "gold sponsor", "silver sponsor",
          "bronze sponsor", "platinum sponsor", "sponsored by", "powered by",
          "community partner", "community partners", "media partner",
          "media partners", "knowledge partner", "platform partner", "and",
          "more", "others", "tba", "tbd", "coming soon", "none", "null", "n/a"}

# Acronyms that look like a campus but are not one.
NOT_A_CAMPUS = {"AND", "THE", "USD", "INR", "API", "STEM", "FOR", "OUR", "ALL",
                "NEW", "TOP", "ACM", "IEEE", "GDSC", "GDG", "CSI"}

TRAILING_JUNK = re.compile(
    r"[,;]?\s*(powered|presented|sponsored|supported|organi[sz]ed)(\s+by)?\s*$",
    re.I)
# The same words on the front. An extractor that reads "Sponsored by Postman"
# off a page hands back the whole phrase as the company name often enough that
# it is worth stripping here rather than living with "Sponsored by Postman" in
# a prospect list a human has to read.
LEADING_JUNK = re.compile(
    r"^\s*(?:(?:proudly\s+)?(?:powered|presented|sponsored|supported|backed"
    r"|organi[sz]ed|brought\s+to\s+you)\s+by|in\s+partnership\s+with"
    r"|our\s+(?:sponsors?|partners?)|title\s+sponsor)\b[:\s]*", re.I)


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def clean_name(name: str) -> str:
    """Reject the debris an extractor produces: '.xyz', 'T- Mobile',
    'Breaking the Glass, powered', 'Devfolio (platform partner)'. A prospect
    list is read by a human, and a fragment in it costs more attention than it
    saves."""
    name = re.sub(r"\s+", " ", (name or "").strip(" \t\n-–—:·|*"))
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()      # "(platform partner)"
    name = LEADING_JUNK.sub("", name).strip(" ,;-")
    name = TRAILING_JUNK.sub("", name).strip(" ,;-")
    name = re.sub(r"(\w)- (\w)", r"\1-\2", name)                # "T- Mobile"
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) < 3 or not re.search(r"[A-Za-z]{3}", name):
        return ""
    if name.startswith(".") or name.count(" ") > 5:
        return ""
    if name.lower() in LABELS:
        return ""
    return name


def _norm(name: str) -> str:
    """'Postman Inc.' and 'postman' are one company. Same rule as the store, so
    a discovered lead collides with the list the same way `add` does."""
    return norm_company(name)


def host_words(event) -> set[str]:
    """The host is not a sponsor of its own event, but its name sits on every
    sponsor page. Returns the club name and its distinctive words, lower-case."""
    club = (getattr(event, "club", "") or "").strip()
    if not club:
        return set()
    out = {club.lower()}
    for word in re.split(r"[\s,/&]+", club.lower()):
        word = word.strip(".-")
        if len(word) >= 3 and word not in HOST_STOPWORDS:
            out.add(word)
    return out


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def _attr(event, name: str) -> str:
    return str(getattr(event, name, "") or "").strip()


def _year(text: str) -> str:
    match = re.search(r"\b(20\d\d)\b", text or "")
    return match.group(1) if match else str(date.today().year)


def _campus_names(colleges: str) -> list[str]:
    """Named campuses in the free-text audience line: 'SVNIT Surat', 'IIT'.
    Only acronyms are trusted; anything else in that field is prose."""
    out: list[str] = []
    for acro, tail in re.findall(r"\b([A-Z]{3,8})(?:\s+([A-Z][a-z]{2,}))?\b",
                                 colleges or ""):
        if acro in NOT_A_CAMPUS:
            continue
        name = "{} {}".format(acro, tail).strip()
        if name not in out:
            out.append(name)
    return out


def default_queries(event) -> list[str]:
    """Queries anchored on the event. Generic queries return generic results:
    the first sweep of the ancestor project pulled ten US companies off one
    women-in-tech blog because nothing in the query said where the event was
    or who it was for. So every query here carries the kind, the place or a
    named campus, and blank event fields simply drop out."""
    kind = _attr(event, "kind")
    city = _attr(event, "city")
    region = _attr(event, "region")
    country = _attr(event, "country")
    year = _year(_attr(event, "date"))
    name = _attr(event, "event_name")
    campuses = _campus_names(_attr(event, "colleges"))

    templates: list[tuple] = [
        (kind, "sponsors", region, country, year),
        (country, "college", kind, "sponsors list"),
        (kind, city, "sponsored by"),
        ("devfolio unstop", country, kind, "title sponsor"),
        ("college tech fest", country, "our sponsors partners"),
    ]
    templates += [(campus, kind, "sponsors") for campus in campuses[:2]]
    if name:
        # Past editions of our own event: the companies on that page have
        # sponsored precisely this, which is the strongest evidence there is.
        templates.append((name, "sponsors"))

    out: list[str] = []
    seen: set[str] = set()
    for parts in templates:
        query = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
        if query and query.lower() not in seen:
            seen.add(query.lower())
            out.append(query)
    return out


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

@dataclass
class Lead:
    company: str
    events: set = field(default_factory=set)
    sources: list = field(default_factory=list)
    website: str = ""            # never guessed; contacts.find_for fills it in

    @property
    def strength(self) -> int:
        return len(self.sources)


def _search_urls(cfg, queries: list[str]) -> list[str]:
    """Unique result URLs, merged round-robin across queries so that with a
    six-page budget every query gets its top result looked at, instead of the
    first query's four results crowding the rest out."""
    columns: list[list[str]] = []
    for query in queries:
        try:
            results = search(cfg, query, k=4)
        except Exception:
            continue
        columns.append([r.get("url", "") for r in results if r.get("url")])
    out: list[str] = []
    for i in range(max((len(c) for c in columns), default=0)):
        for column in columns:
            if i < len(column) and column[i] not in out:
                out.append(column[i])
    return out


def _is_aggregator(url: str) -> bool:
    """research.is_aggregator when the research module offers it; a page that
    the researcher would refuse to cite is not worth a fetch here either."""
    check = getattr(research_mod, "is_aggregator", None)
    if not callable(check):
        return False
    try:
        return bool(check(url))
    except Exception:
        return False


def discover(cfg, llm, event, queries=None, max_pages: int = 6, db=None,
             run_id=None) -> list[Lead]:
    """Fetch sponsor pages of comparable events and rank the companies on them.

    Explicit `queries` are searched first and in full; the event's default
    queries only fill whatever page budget is left, so `--query` on the
    command line means what the user thinks it means.
    """
    extra = [q for q in (queries or []) if str(q).strip()]
    urls = _search_urls(cfg, extra)
    for url in _search_urls(cfg, default_queries(event)):
        if url not in urls:
            urls.append(url)
    urls = [u for u in urls if not _is_aggregator(u)]

    banned = {_norm(x) for x in NOT_A_SPONSOR | host_words(event)}
    leads: dict[str, Lead] = {}
    for url in urls[:max_pages]:
        try:
            _, text = fetch_url(cfg, url)
        except Exception:
            continue                        # one dead page is not a reason to stop
        try:
            data, _ = structured(
                llm, system=prompts.EXTRACT_SPONSORS_SYSTEM,
                user=prompts.EXTRACT_SPONSORS_USER.format(url=url, text=text[:6000]),
                temperature=0.0, db=db, run_id=run_id, kind="extract_sponsors",
                target=url[:200])
        except QuotaExhausted:
            # "0 companies found" would be a lie: we never got to look.
            raise
        except Exception:
            continue
        event_name = str(data.get("event") or url)[:80]
        sponsors = data.get("sponsors") or []
        if not isinstance(sponsors, list):
            sponsors = [sponsors]
        for raw in sponsors:
            name = clean_name(str(raw))
            if not name:
                continue
            key = _norm(name)
            if not key or key in banned:
                continue
            lead = leads.setdefault(key, Lead(company=name))
            lead.events.add(event_name)
            if url not in lead.sources:
                lead.sources.append(url)
    return sorted(leads.values(), key=lambda lead: (-lead.strength,
                                                    lead.company.lower()))


def already_known(db, company: str) -> bool:
    return find_by_company(db, company) is not None
