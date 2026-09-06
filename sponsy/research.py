"""Research: search, fetch, excerpt. The closed world the drafter is allowed.

Everything the drafter may later assert about a company has to pass through
here first and land in the `research` table with the URL it came from. There is
no other channel: the drafting prompt gets excerpts, not the internet, so a
claim that cannot be traced to a row in that table is by construction invented.

Two budgets live inside the tools themselves rather than in the prompt, so they
hold even when the model ignores its instructions: at most 3 searches and at
most `cfg.max_fetches` pages per prospect. robots.txt is honoured -- we are
about to email these people, and being a rude crawler first is a bad opening.

Frozen mode (`frozen=[Excerpt, ...]`) serves search results and pages from the
fixtures only. It is how tests, evals and `demo` run the real tool loop with no
network and byte-stable output.
"""

from __future__ import annotations

import codecs
import html as htmllib
import re
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser

from . import prompts, store
from .agent import AgentRun, run_agent
from .models import Excerpt, Research
from .tools import ToolError, ToolRegistry

EXCERPT_CHARS = 2000
MAX_BYTES = 2_000_000
MAX_SEARCHES = 3
MIN_TEXT_CHARS = 120
SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg",
             "form", "aside"}
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
              "tr", "section", "article", "blockquote", "pre", "table", "ul",
              "ol", "dl", "dd", "dt", "main", "figure", "figcaption"}
# Paragraphs containing one of these survive excerpting even when they sit far
# below the fold: they are what a sponsorship email is written from.
INTEREST = ("student", "campus", "college", "university", "intern", "hackathon",
            "fest", "sponsor", "scholarship", "community", "hiring", "career",
            "developer", "education", "learner")

# Sites that describe a company second-hand, or that serve a login wall / a
# JS shell to a crawler. A fetch of one of these is usually a wasted slot out
# of four, so the tool description steers the model away and search results
# carry an "aggregator" flag. They are not blocked outright: sometimes a news
# piece is the only source there is.
AGGREGATOR_DOMAINS = (
    "wikipedia.org", "linkedin.com", "crunchbase.com", "youtube.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "glassdoor.com",
    "indeed.com", "g2.com", "reddit.com", "medium.com", "bloomberg.com",
    "forbes.com", "techcrunch.com", "zoominfo.com", "rocketreach.co",
    "apollo.io", "pitchbook.com",
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def host_of(url: str) -> str:
    """Lower-cased host without port or a leading www. Empty when unparseable."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url                 # bare "linkedin.com/company/x"
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_aggregator(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in AGGREGATOR_DOMAINS)


def _key(url: str) -> str:
    """Comparison key: same page, different spelling -> same key."""
    url = (url or "").strip().split("#", 1)[0]
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/",
         parts.query, "")).rstrip("/")


def _looks_like_domain(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", value or ""))


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

class _Extractor(HTMLParser):
    """Readable text only: skipped subtrees are chrome (nav, footer, forms) or
    not text at all (script, style, svg). Block boundaries become newlines so
    the excerpter can reason in paragraphs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            # Only the document title; an <svg><title> would be caught by the
            # skip counter, and a second <title> is somebody's markup bug.
            self._in_title = not self._skip and not self.title.strip()
        elif tag in BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self._skip:
                self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            text = data.strip()
            if text:
                self.chunks.append(text + " ")

    def text(self) -> str:
        joined = "".join(self.chunks)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{2,}", "\n", joined)
        return joined.strip()


def html_to_text(html: str) -> tuple[str, str]:
    parser = _Extractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:                          # noqa: BLE001  half a page beats none
        pass
    title = re.sub(r"\s+", " ", parser.title).strip()
    return title, parser.text()


def make_excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Head of the page plus any paragraph further down that looks
    student-relevant. The head carries what the company is; the interest
    paragraphs carry why a student club should write to it."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    cut = head.rfind(" ")
    if cut > limit * 0.4:                      # do not end mid-word
        head = head[:cut]
    tail_budget = limit - len(head)
    picked: list[str] = []
    used = 0
    for para in text[len(head):].split("\n"):
        para = para.strip()
        low = para.lower()
        if len(para) > 40 and any(word in low for word in INTEREST):
            picked.append(para)
            used += len(para) + 1
            if used >= tail_budget:
                break
    tail = "\n".join(picked)[:tail_budget].rstrip()
    return head.rstrip() + ("\n...\n" + tail if tail else "")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

# host root -> parser. One robots.txt request per host per process.
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}


def _load_robots(cfg, root: str) -> urllib.robotparser.RobotFileParser:
    """Fetch robots.txt ourselves rather than RobotFileParser.read(): that
    path uses urlopen with no timeout and the default Python user agent."""
    import requests  # noqa: PLC0415

    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(root + "/robots.txt")
    resp = requests.get(root + "/robots.txt", timeout=cfg.fetch_timeout,
                        headers={"User-Agent": cfg.user_agent})
    try:
        status = int(resp.status_code)
        if status in (401, 403):
            parser.disallow_all = True         # same reading as the stdlib
        elif 400 <= status < 500:
            parser.allow_all = True            # no robots.txt: everything goes
        elif status >= 500:
            raise RuntimeError("robots.txt returned HTTP {}".format(status))
        else:
            parser.parse((resp.text or "").splitlines())
    finally:
        resp.close()
    return parser


def robots_allows(cfg, url: str) -> bool:
    """False only when a robots.txt we could read says so. Anything that goes
    wrong on the way (no file, timeout, 5xx, odd URL) means proceed: the page
    fetch itself still has to succeed."""
    try:
        parts = urllib.parse.urlsplit(url)
        root = "{}://{}".format(parts.scheme, parts.netloc).lower()
        parser = _ROBOTS.get(root)
        if parser is None:
            parser = _load_robots(cfg, root)
            _ROBOTS[root] = parser
        return parser.can_fetch(cfg.user_agent, url)
    except Exception:                          # noqa: BLE001
        return True


def _charset(ctype: str, body: bytes) -> str:
    match = re.search(r"charset=[\"']?([\w.:-]+)", ctype or "", re.I)
    if not match:
        head = body[:4096].decode("ascii", errors="ignore")
        match = re.search(r"<meta[^>]+charset=[\"']?([\w.:-]+)", head, re.I)
    name = match.group(1) if match else "utf-8"
    try:
        codecs.lookup(name)
    except LookupError:
        return "utf-8"
    return name


def fetch_url(cfg, url: str) -> tuple[str, str]:
    """One page, politely: robots checked, our user agent, a timeout, 2 MB cap,
    text only. Every refusal is a ToolError with a message the model can act
    on, because it is the model that reads it."""
    import requests  # noqa: PLC0415

    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("only http(s) URLs can be fetched, not {!r}".format(url[:80]))
    if not robots_allows(cfg, url):
        raise ToolError("robots.txt disallows fetching {}; pick another page"
                        .format(url))
    try:
        resp = requests.get(url, timeout=cfg.fetch_timeout, stream=True,
                            headers={"User-Agent": cfg.user_agent,
                                     "Accept": "text/html,text/plain;q=0.9"})
    except requests.RequestException as exc:
        raise ToolError("could not fetch {}: {}".format(url, exc)) from exc
    try:
        status = int(resp.status_code)
        if status >= 400:
            raise ToolError("HTTP {} for {}".format(status, url))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" not in ctype and not ctype.startswith("text/"):
            raise ToolError(
                "not a text page (content-type {}); only HTML and plain text can "
                "be read".format(ctype or "unknown"))
        body = resp.raw.read(MAX_BYTES, decode_content=True) or b""
    finally:
        resp.close()

    decoded = body.decode(_charset(ctype, body), errors="replace")
    if "html" in ctype:
        title, text = html_to_text(decoded)
    else:
        title, text = "", re.sub(r"[ \t]+", " ", decoded).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ToolError("page had almost no readable text (JS-rendered?); try "
                        "another page on the same site")
    return title or url, text


# ---------------------------------------------------------------------------
# search providers
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def _clean(fragment: str, limit: int = 300) -> str:
    text = htmllib.unescape(_TAG.sub("", fragment or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def search(cfg, query: str, k: int = 5) -> list[dict]:
    """[{title, url, snippet}] from whichever provider the config resolves to.
    `none` returns nothing rather than failing, so an offline run is a run with
    no search results, not a crash."""
    provider = cfg.resolved_search_provider()
    if provider == "none":
        return []
    import requests  # noqa: PLC0415

    if provider == "tavily":
        if not cfg.tavily_api_key:
            raise ToolError("SEARCH_PROVIDER=tavily but TAVILY_API_KEY is not set")
        resp = requests.post("https://api.tavily.com/search", timeout=cfg.fetch_timeout,
                             json={"api_key": cfg.tavily_api_key, "query": query,
                                   "max_results": k, "search_depth": "basic"})
        resp.raise_for_status()
        return [{"title": _clean(r.get("title", ""), 200), "url": r.get("url", ""),
                 "snippet": _clean(r.get("content") or "")}
                for r in (resp.json().get("results") or [])[:k] if r.get("url")]

    if provider == "brave":
        if not cfg.brave_api_key:
            raise ToolError("SEARCH_PROVIDER=brave but BRAVE_API_KEY is not set")
        resp = requests.get("https://api.search.brave.com/res/v1/web/search",
                            timeout=cfg.fetch_timeout, params={"q": query, "count": k},
                            headers={"X-Subscription-Token": cfg.brave_api_key,
                                     "Accept": "application/json"})
        resp.raise_for_status()
        results = ((resp.json().get("web") or {}).get("results") or [])[:k]
        return [{"title": _clean(r.get("title", ""), 200), "url": r.get("url", ""),
                 "snippet": _clean(r.get("description") or "")}
                for r in results if r.get("url")]

    # Keyless fallback: DuckDuckGo's HTML endpoint, parsed with regexes. Fragile
    # by nature; when it stops parsing, set SEARCH_PROVIDER=tavily and add a key.
    resp = requests.post("https://html.duckduckgo.com/html/", timeout=cfg.fetch_timeout,
                         data={"q": query}, headers={"User-Agent": cfg.user_agent})
    resp.raise_for_status()
    return _parse_ddg(resp.text, k)


_DDG_LINK = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S)


def _parse_ddg(page: str, k: int) -> list[dict]:
    out: list[dict] = []
    matches = list(_DDG_LINK.finditer(page or ""))
    for i, match in enumerate(matches):
        href = htmllib.unescape(match.group("href"))
        redirect = re.search(r"uddg=([^&]+)", href)
        if redirect:
            href = urllib.parse.unquote(redirect.group(1))
        if not href.startswith("http"):
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        snip = _DDG_SNIPPET.search(page, match.end(), end)
        out.append({"title": _clean(match.group("title"), 200), "url": href,
                    "snippet": _clean(snip.group("snippet")) if snip else ""})
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# the tools the research agent gets
# ---------------------------------------------------------------------------

@dataclass
class ResearchTools:
    cfg: object
    db: object
    prospect: object
    run_id: int | None = None
    frozen: list[Excerpt] | None = None       # evals / offline: no network
    fetched: dict[str, Excerpt] = field(default_factory=dict)
    searches: int = 0

    @property
    def budget_left(self) -> int:
        return max(0, int(self.cfg.max_fetches) - len(self.fetched))

    def _already(self, url: str) -> Excerpt | None:
        key = _key(url)
        for known, excerpt in self.fetched.items():
            if _key(known) == key:
                return excerpt
        return None

    def web_search(self, query: str) -> dict:
        self.searches += 1
        if self.searches > MAX_SEARCHES:
            raise ToolError("search budget spent ({} searches); fetch a page or "
                            "answer now".format(MAX_SEARCHES))
        if self.frozen is not None:
            raw = [{"title": e.title, "url": e.url, "snippet": e.text[:200]}
                   for e in self.frozen]
        else:
            try:
                raw = search(self.cfg, query, k=5)
            except ToolError:
                raise
            except Exception as exc:           # noqa: BLE001  network, JSON, 5xx
                raise ToolError("search failed ({}: {}); try a simpler query or "
                                "fetch a page you already know"
                                .format(type(exc).__name__, exc)) from exc
        results: list[dict] = []
        seen: set[str] = set()
        for r in raw:
            url = (r.get("url") or "").strip()
            if not url or _key(url) in seen:
                continue
            seen.add(_key(url))
            results.append({"title": r.get("title", ""), "url": url,
                            "snippet": r.get("snippet", ""),
                            "aggregator": is_aggregator(url)})
        if not results:
            return {"results": [], "note": "no results; try a simpler query"}
        return {"results": results}

    def fetch_page(self, url: str) -> dict:
        url = (url or "").strip()
        previous = self._already(url)
        if previous is not None:               # free: nothing new is fetched
            return {"url": previous.url, "title": previous.title,
                    "text": previous.text, "note": "already fetched",
                    "fetches_left": self.budget_left}
        if self.budget_left <= 0:
            raise ToolError(
                "fetch budget spent ({} pages). Answer with what you have."
                .format(self.cfg.max_fetches))
        if self.frozen is not None:
            match = next((e for e in self.frozen if _key(e.url) == _key(url)), None)
            if match is None:
                raise ToolError("no frozen source for {}; offline, only URLs from "
                                "web_search results can be fetched".format(url))
            url, title, text = match.url, match.title, match.text
        else:
            title, text = fetch_url(self.cfg, url)
        excerpt = Excerpt(url=url, title=title, text=make_excerpt(text))
        self.fetched[url] = excerpt
        store.save_excerpt(self.db, self.prospect.id, self.run_id, url, title,
                           excerpt.text)
        return {"url": url, "title": title, "text": excerpt.text,
                "fetches_left": self.budget_left}

    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.add(
            "web_search",
            "Search the web. Returns up to 5 results, each with title, url, "
            "snippet and an \"aggregator\" flag. Put the company name together "
            "with \"students\", \"hackathon\" or \"developer community\" in the "
            "query to find campus programmes and past sponsorships; a second "
            "query can target their own site. At most {} searches per company."
            .format(MAX_SEARCHES),
            {"type": "object",
             "properties": {"query": {"type": "string",
                                      "description": "search query"}},
             "required": ["query"]},
            self.web_search)
        reg.add(
            "fetch_page",
            "Fetch one URL and return its readable text. Hard cap: {} pages per "
            "company, so choose well. Prefer the company's OWN website (about, "
            "community, education, careers, newsroom pages). Do NOT fetch "
            "aggregator, social or news domains, which waste a slot: {}. Results "
            "from those come back flagged \"aggregator\": true. A page that "
            "fails to load does not use up the budget."
            .format(self.cfg.max_fetches, ", ".join(AGGREGATOR_DOMAINS)),
            {"type": "object",
             "properties": {"url": {"type": "string",
                                    "description": "absolute http(s) URL"}},
             "required": ["url"]},
            self.fetch_page)
        return reg


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _confidence(value) -> float:
    try:
        conf = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if conf != conf:                           # NaN
        return 0.0
    return min(1.0, max(0.0, conf))


def _normalise(data) -> dict:
    """Coerce whatever the model returned into what Research.from_json expects;
    a stray string where a list should be must not take the run down."""
    if not isinstance(data, dict):
        return {}
    data = dict(data)
    sources = data.get("sources_used")
    if isinstance(sources, str):
        sources = [sources]
    data["sources_used"] = [s.strip() for s in (sources or [])
                           if isinstance(s, str) and s.strip()]
    data["confidence"] = _confidence(data.get("confidence"))
    for key in ("one_liner", "student_relevance", "prior_campus_sponsorship",
                "recent_signal", "hook", "company_domain"):
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            val = str(val)
        data[key] = val.strip() if isinstance(val, str) else None
    return data


def research_prospect(llm, cfg, db, prospect, event, run_id: int | None = None,
                      frozen: list[Excerpt] | None = None
                      ) -> tuple[Research, list[Excerpt], AgentRun]:
    """Run the bounded agent loop, then normalise its JSON into a Research.

    The model's own view of its work is trusted only where it can be checked:
    sources it claims but did not fetch are dropped, no fetched page means
    confidence zero, and running out of budget caps confidence at 0.5 because
    the answer, if any, came out of a truncated process."""
    tools = ResearchTools(cfg=cfg, db=db, prospect=prospect, run_id=run_id,
                          frozen=frozen)
    system = prompts.RESEARCH_SYSTEM.format(max_fetches=cfg.max_fetches)
    user = prompts.RESEARCH_USER.format(
        company=prospect.company,
        website=prospect.website or "(unknown)",
        contact_name=prospect.contact_name or "(unknown)",
        segment=prospect.segment or "(unspecified)",
        notes=prospect.notes or "(none)",
        event_name=event.event_name, date=event.date,
        where=event.where() or "(location not set)",
        summary=event.summary or "", colleges=event.colleges)

    run = run_agent(llm, system=system, user=user, registry=tools.registry(),
                    max_steps=cfg.max_steps, max_cost=cfg.max_cost_usd,
                    db=db, run_id=run_id, target=prospect.id,
                    raise_on_budget=False)

    excerpts = list(tools.fetched.values())
    try:
        data = _normalise(run.json())
    except Exception:                          # noqa: BLE001  junk or empty text
        data = {}

    research = Research.from_json(prospect.id, data)

    by_key = {_key(e.url): e.url for e in excerpts}
    claimed: list[str] = []
    for url in research.sources_used:
        real = by_key.get(_key(url))
        if real and real not in claimed:
            claimed.append(real)
    research.sources_used = claimed or [e.url for e in excerpts]

    confidence = research.confidence
    if not excerpts:
        confidence = 0.0
    elif run.hit_budget:
        confidence = min(confidence, 0.5)
    research.confidence = round(confidence, 2)

    domain = host_of(research.company_domain) if research.company_domain else ""
    if not _looks_like_domain(domain):
        domain = ""
        for e in excerpts:
            if not is_aggregator(e.url):
                domain = host_of(e.url)
                break
        if not domain and getattr(prospect, "website", ""):
            domain = host_of(prospect.website)
    research.company_domain = domain

    if db is not None:
        db.log_action(run_id, "research", prospect.id,
                      {"summary": research.to_json(), "fetched": len(excerpts),
                       "searches": tools.searches, "hit_budget": run.hit_budget},
                      confidence=research.confidence)
    return research, excerpts, run
