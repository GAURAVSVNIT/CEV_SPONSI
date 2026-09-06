"""research.py: the closed world. Every test here is offline; requests is
monkeypatched wherever the code would otherwise reach the network."""

from __future__ import annotations

import json

import pytest
import requests

from sponsy import research
from sponsy.llm import FakeLLM, LLMResponse, ToolCall
from sponsy.models import Excerpt
from sponsy.tools import ToolError
from tests.conftest import SOURCES


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, n: int = -1, decode_content: bool = True) -> bytes:
        return self.body[:n] if n and n > 0 else self.body


class FakeResponse:
    """Just enough of requests.Response for research.py."""

    def __init__(self, body: str | bytes = "", status: int = 200,
                 ctype: str = "text/html; charset=utf-8", payload=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.encoding = "utf-8"
        self.payload = payload
        self.raw = _Raw(self.body)
        self.closed = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP {}".format(self.status_code))

    def close(self):
        self.closed = True


def _no_network(*args, **kwargs):
    raise AssertionError("network call attempted: {} {}".format(args, kwargs))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Default stance: any requests call is a test failure. Tests that need a
    canned response override the specific function."""
    monkeypatch.setattr(requests, "get", _no_network)
    monkeypatch.setattr(requests, "post", _no_network)
    research._ROBOTS.clear()
    yield
    research._ROBOTS.clear()


PAGE = """<html><head><title> Acme  Analytics </title>
<script>var x = "SCRIPT_TEXT";</script><style>.a{color:red}</style></head>
<body><nav><a href="/">NAV_LINK</a></nav><header>HEADER_TEXT</header>
<main><h1>About Acme</h1><p>Acme Analytics builds a data   analytics workbench
for product teams.</p><p>The company runs a student programme that gives free
licences to college societies and has sponsored several college hackathons.</p>
<ul><li>one</li><li>two</li></ul></main>
<aside>ASIDE_TEXT</aside><form><input name="q"></form>
<footer>FOOTER_TEXT</footer><noscript>NOSCRIPT_TEXT</noscript>
<svg><title>icon</title></svg></body></html>"""


# ---------------------------------------------------------------------------
# html -> text, excerpting
# ---------------------------------------------------------------------------

def test_html_to_text_drops_chrome_and_keeps_title():
    title, text = research.html_to_text(PAGE)
    assert title == "Acme Analytics"
    for junk in ("SCRIPT_TEXT", "NAV_LINK", "HEADER_TEXT", "ASIDE_TEXT",
                 "FOOTER_TEXT", "NOSCRIPT_TEXT", "color:red", "icon"):
        assert junk not in text
    assert "Acme Analytics builds a data analytics workbench" in text
    assert "sponsored several college hackathons" in text


def test_html_to_text_newlines_on_blocks_and_collapsed_whitespace():
    _, text = research.html_to_text(PAGE)
    lines = text.split("\n")
    assert lines[0] == "About Acme"
    assert "one" in lines and "two" in lines           # each <li> on its own line
    assert "  " not in text and "\n\n" not in text


def test_html_to_text_survives_garbage():
    title, text = research.html_to_text("<<<>>> not <b really html </")
    assert title == ""
    assert isinstance(text, str)


def test_make_excerpt_short_text_is_unchanged():
    assert research.make_excerpt("short text", limit=100) == "short text"


def test_make_excerpt_keeps_student_paragraph_from_beyond_the_head():
    filler = "\n".join("Paragraph {} is about logistics dashboards and nothing "
                       "else worth quoting to anybody.".format(i) for i in range(60))
    student = ("Every year the company sponsors a campus hackathon and offers "
               "internships to student teams who build on its API.")
    text = filler + "\n" + student + "\n" + "Trailing legal boilerplate. " * 20
    assert len(text) > 4000
    excerpt = research.make_excerpt(text, limit=2000)
    assert len(excerpt) <= 2000 + len("\n...\n")
    assert student in excerpt
    assert "\n...\n" in excerpt
    assert "boilerplate" not in excerpt
    assert not excerpt.split("\n...\n")[0].endswith("Paragrap")   # no mid-word cut


# ---------------------------------------------------------------------------
# aggregator detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/company/acme", True),
    ("https://en.wikipedia.org/wiki/Acme", True),
    ("https://x.com/acme", True),
    ("https://medium.com/@acme/post", True),
    ("linkedin.com/company/acme", True),                 # no scheme
    ("https://acme-analytics.example.com/about", False),
    ("https://notx.com/acme", False),                    # suffix, not subdomain
    ("https://forbes.com.example.org/", False),
    ("", False),
])
def test_is_aggregator(url, expected):
    assert research.is_aggregator(url) is expected


# ---------------------------------------------------------------------------
# robots
# ---------------------------------------------------------------------------

def test_robots_allows_returns_true_when_fetch_raises(cfg, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "get", boom)
    assert research.robots_allows(cfg, "https://unreachable.example.com/page") is True
    assert research.robots_allows(cfg, "not a url at all") is True


def test_robots_disallow_is_honoured_and_cached(cfg, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["User-Agent"] == cfg.user_agent
        return FakeResponse("User-agent: *\nDisallow: /private\n", ctype="text/plain")
    monkeypatch.setattr(requests, "get", fake_get)
    assert research.robots_allows(cfg, "https://h.example.com/private/x") is False
    assert research.robots_allows(cfg, "https://h.example.com/public") is True
    assert calls == ["https://h.example.com/robots.txt"]     # one read per host


def test_robots_404_means_allowed(cfg, monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: FakeResponse("nope", status=404))
    assert research.robots_allows(cfg, "https://h.example.com/anything") is True


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------

def _serve(monkeypatch, pages: dict, robots: str | None = None):
    """requests.get that answers robots.txt and the given pages only."""
    def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            if robots is None:
                return FakeResponse("", status=404)
            return FakeResponse(robots, ctype="text/plain")
        if url in pages:
            return pages[url]
        return FakeResponse("", status=404)
    monkeypatch.setattr(requests, "get", fake_get)


def test_fetch_url_returns_title_and_text(cfg, monkeypatch):
    _serve(monkeypatch, {"https://acme.example.com/about": FakeResponse(PAGE)})
    title, text = research.fetch_url(cfg, "https://acme.example.com/about")
    assert title == "Acme Analytics"
    assert "student programme" in text
    assert "NAV_LINK" not in text


def test_fetch_url_rejects_non_http(cfg):
    with pytest.raises(ToolError, match="http"):
        research.fetch_url(cfg, "ftp://acme.example.com/file")
    with pytest.raises(ToolError):
        research.fetch_url(cfg, "javascript:alert(1)")


def test_fetch_url_honours_robots(cfg, monkeypatch):
    _serve(monkeypatch, {"https://acme.example.com/private/x": FakeResponse(PAGE)},
           robots="User-agent: *\nDisallow: /private\n")
    with pytest.raises(ToolError, match="robots"):
        research.fetch_url(cfg, "https://acme.example.com/private/x")


def test_fetch_url_rejects_non_text(cfg, monkeypatch):
    _serve(monkeypatch, {"https://acme.example.com/deck.pdf":
                         FakeResponse(b"%PDF-1.4 " * 100, ctype="application/pdf")})
    with pytest.raises(ToolError, match="not a text page"):
        research.fetch_url(cfg, "https://acme.example.com/deck.pdf")


def test_fetch_url_rejects_js_shell(cfg, monkeypatch):
    shell = "<html><head><title>App</title></head><body><div id='root'></div>" \
            "<script src='/bundle.js'></script></body></html>"
    _serve(monkeypatch, {"https://acme.example.com/": FakeResponse(shell)})
    with pytest.raises(ToolError, match="JS-rendered"):
        research.fetch_url(cfg, "https://acme.example.com/")


def test_fetch_url_http_error_is_readable(cfg, monkeypatch):
    _serve(monkeypatch, {})
    with pytest.raises(ToolError, match="HTTP 404"):
        research.fetch_url(cfg, "https://acme.example.com/missing")


def test_fetch_url_caps_body_at_two_megabytes(cfg, monkeypatch):
    huge = "<html><body><p>" + ("word " * 600_000) + "</p></body></html>"
    assert len(huge) > research.MAX_BYTES
    _serve(monkeypatch, {"https://acme.example.com/big": FakeResponse(huge)})
    _, text = research.fetch_url(cfg, "https://acme.example.com/big")
    assert len(text) <= research.MAX_BYTES


# ---------------------------------------------------------------------------
# search providers
# ---------------------------------------------------------------------------

def test_search_none_returns_nothing(cfg):
    cfg.search_provider = "none"
    assert research.search(cfg, "acme students") == []


def test_search_tavily(cfg, monkeypatch):
    cfg.search_provider, cfg.tavily_api_key = "tavily", "tv-key"
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(url=url, json=kwargs["json"])
        return FakeResponse(payload={"results": [
            {"title": "Acme for students", "url": "https://acme.example.com/students",
             "content": "Acme runs a student programme " * 30},
            {"title": "Acme on LinkedIn", "url": "https://linkedin.com/company/acme",
             "content": "profile"},
        ]})
    monkeypatch.setattr(requests, "post", fake_post)
    out = research.search(cfg, "acme students", k=5)
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["json"]["api_key"] == "tv-key"
    assert seen["json"]["query"] == "acme students"
    assert seen["json"]["max_results"] == 5
    assert seen["json"]["search_depth"] == "basic"
    assert [r["url"] for r in out] == ["https://acme.example.com/students",
                                       "https://linkedin.com/company/acme"]
    assert out[0]["title"] == "Acme for students"
    assert len(out[0]["snippet"]) <= 300
    assert set(out[0]) == {"title", "url", "snippet"}


def test_search_brave(cfg, monkeypatch):
    cfg.search_provider, cfg.brave_api_key = "brave", "br-key"
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(url=url, params=kwargs["params"], headers=kwargs["headers"])
        return FakeResponse(payload={"web": {"results": [
            {"title": "Acme &amp; students", "url": "https://acme.example.com/",
             "description": "<strong>Acme</strong> sponsors hackathons"}]}})
    monkeypatch.setattr(requests, "get", fake_get)
    out = research.search(cfg, "acme hackathon", k=3)
    assert seen["url"].startswith("https://api.search.brave.com/res/v1/web/search")
    assert seen["headers"]["X-Subscription-Token"] == "br-key"
    assert seen["params"] == {"q": "acme hackathon", "count": 3}
    assert out == [{"title": "Acme & students", "url": "https://acme.example.com/",
                    "snippet": "Acme sponsors hackathons"}]


def test_search_ddg_parses_redirect_links(cfg, monkeypatch):
    cfg.search_provider = "ddg"
    page = ('<div class="result"><a rel="nofollow" class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example.com%2Fstudents'
            '&amp;rut=abc">Acme <b>students</b></a>'
            '<a class="result__snippet" href="x">Free licences for <b>college</b> '
            'clubs</a></div>'
            '<div class="result"><a class="result__a" href="https://other.example.com/">'
            'Other</a></div>')
    monkeypatch.setattr(requests, "post", lambda url, **k: FakeResponse(page))
    out = research.search(cfg, "acme students", k=5)
    assert out[0] == {"title": "Acme students", "url": "https://acme.example.com/students",
                      "snippet": "Free licences for college clubs"}
    assert out[1]["url"] == "https://other.example.com/"


def test_search_provider_without_key_fails_readably(cfg):
    cfg.search_provider, cfg.tavily_api_key = "tavily", ""
    with pytest.raises(ToolError, match="TAVILY_API_KEY"):
        research.search(cfg, "acme")


# ---------------------------------------------------------------------------
# ResearchTools: budgets, frozen mode, persistence
# ---------------------------------------------------------------------------

def test_fetches_are_capped_per_prospect(cfg, db, prospect):
    many = [Excerpt(url="https://example.com/{}".format(i), title=str(i),
                    text="text " * 40) for i in range(6)]
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, frozen=many)
    reg = tools.registry()
    for i in range(cfg.max_fetches):
        out = reg.call("fetch_page", {"url": many[i].url})
        assert "error" not in out
        assert out["fetches_left"] == cfg.max_fetches - i - 1
    blocked = reg.call("fetch_page", {"url": many[5].url})
    assert "fetch budget spent" in blocked["error"]
    assert tools.budget_left == 0
    assert db.one("SELECT COUNT(*) n FROM research WHERE prospect_id='P01'")["n"] \
        == cfg.max_fetches
    row = db.one("SELECT source_url, title, excerpt FROM research ORDER BY id LIMIT 1")
    assert row["source_url"] == many[0].url and row["title"] == "0"
    assert row["excerpt"].startswith("text text")


def test_search_budget_is_capped(cfg, db, prospect):
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, frozen=SOURCES)
    reg = tools.registry()
    for _ in range(3):
        assert "error" not in reg.call("web_search", {"query": "x"})
    fourth = reg.call("web_search", {"query": "x"})
    assert "search budget spent" in fourth["error"]
    assert tools.searches == 4


def test_frozen_mode_serves_fixtures_and_never_touches_network(cfg, db, prospect):
    # requests.get / post raise via the autouse fixture: any network use fails.
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, frozen=SOURCES)
    reg = tools.registry()
    found = reg.call("web_search", {"query": "Acme Analytics students"})
    assert [r["url"] for r in found["results"]] == [s.url for s in SOURCES]
    assert all(r["aggregator"] is False for r in found["results"])
    page = reg.call("fetch_page", {"url": SOURCES[0].url})
    assert page["title"] == SOURCES[0].title
    assert page["text"] == SOURCES[0].text
    assert page["fetches_left"] == cfg.max_fetches - 1
    off = reg.call("fetch_page", {"url": "https://elsewhere.example.com/"})
    assert "no frozen source" in off["error"]
    assert tools.budget_left == cfg.max_fetches - 1        # a refusal is free
    assert list(tools.fetched) == [SOURCES[0].url]


def test_repeat_fetch_is_free_and_marked(cfg, db, prospect):
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, frozen=SOURCES)
    reg = tools.registry()
    first = reg.call("fetch_page", {"url": SOURCES[0].url})
    again = reg.call("fetch_page", {"url": SOURCES[0].url + "#section"})
    assert again["note"] == "already fetched"
    assert again["text"] == first["text"]
    assert again["fetches_left"] == first["fetches_left"]
    assert db.one("SELECT COUNT(*) n FROM research")["n"] == 1


def test_web_search_dedupes_and_flags_aggregators(cfg, db, prospect):
    frozen = [
        Excerpt(url="https://acme.example.com/about", title="About", text="a " * 50),
        Excerpt(url="https://acme.example.com/about/", title="About again",
                text="b " * 50),
        Excerpt(url="https://www.linkedin.com/company/acme", title="LinkedIn",
                text="c " * 50),
    ]
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, frozen=frozen)
    out = tools.registry().call("web_search", {"query": "acme"})
    urls = [r["url"] for r in out["results"]]
    assert urls == ["https://acme.example.com/about",
                    "https://www.linkedin.com/company/acme"]
    assert [r["aggregator"] for r in out["results"]] == [False, True]
    assert all(len(r["snippet"]) <= 200 for r in out["results"])


def test_registry_descriptions_steer_the_model(cfg, db, prospect):
    decls = {d["name"]: d for d in
             research.ResearchTools(cfg, db, prospect).registry().declarations()}
    assert set(decls) == {"web_search", "fetch_page"}
    ws = decls["web_search"]["description"]
    assert "students" in ws and "hackathon" in ws and "developer community" in ws
    fp = decls["fetch_page"]["description"]
    assert "own website" in fp.lower() or "own site" in fp.lower()
    assert "NOT" in fp
    for dom in research.AGGREGATOR_DOMAINS:
        assert dom in fp
    assert str(cfg.max_fetches) in fp
    assert decls["fetch_page"]["parameters"]["required"] == ["url"]
    assert decls["web_search"]["parameters"]["required"] == ["query"]


def test_tool_errors_come_back_as_values_not_exceptions(cfg, db, prospect, monkeypatch):
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect)   # live mode
    reg = tools.registry()
    bad = reg.call("fetch_page", {"url": "ftp://acme.example.com/"})
    assert "only http(s)" in bad["error"]
    cfg.search_provider, cfg.tavily_api_key = "tavily", "k"

    def down(*a, **k):
        raise requests.ConnectionError("search is down")
    monkeypatch.setattr(requests, "post", down)
    failed = reg.call("web_search", {"query": "acme"})
    assert "search failed" in failed["error"] and "search is down" in failed["error"]
    assert tools.budget_left == cfg.max_fetches
    assert db.one("SELECT COUNT(*) n FROM research")["n"] == 0


def test_live_fetch_goes_through_fetch_url_and_saves_excerpt(cfg, db, prospect,
                                                              monkeypatch):
    _serve(monkeypatch, {"https://acme.example.com/about": FakeResponse(PAGE)})
    tools = research.ResearchTools(cfg=cfg, db=db, prospect=prospect, run_id=7)
    out = tools.registry().call("fetch_page", {"url": "https://acme.example.com/about"})
    assert out["title"] == "Acme Analytics"
    assert "student programme" in out["text"]
    row = db.one("SELECT run_id, source_url, excerpt FROM research")
    assert row["run_id"] == 7 and row["source_url"] == "https://acme.example.com/about"
    assert row["excerpt"] == out["text"]


# ---------------------------------------------------------------------------
# research_prospect: the loop end to end on the frozen SOURCES
# ---------------------------------------------------------------------------

def test_research_prospect_on_frozen_sources(llm, cfg, db, prospect, event):
    summary, excerpts, run = research.research_prospect(
        llm, cfg, db, prospect, event, run_id=None, frozen=SOURCES)
    assert sorted(summary.sources_used) == sorted(s.url for s in SOURCES)
    assert summary.confidence > 0.4
    assert summary.hook
    assert summary.company_domain == "acme-analytics.example.com"
    assert summary.prospect_id == "P01"
    assert not summary.thin
    assert {e.url for e in excerpts} == {s.url for s in SOURCES}
    assert run.steps >= 3 and not run.hit_budget
    # the closed world is on disk, one row per fetched page
    assert db.one("SELECT COUNT(*) n FROM research WHERE prospect_id='P01'")["n"] == 2
    kinds = [r["kind"] for r in db.query("SELECT kind FROM actions ORDER BY id")]
    assert "tool:web_search" in kinds and "tool:fetch_page" in kinds
    assert kinds[-1] == "research"


def test_research_prospect_prompt_carries_event_and_prospect(llm, cfg, db, prospect,
                                                              event):
    research.research_prospect(llm, cfg, db, prospect, event, frozen=SOURCES)
    first = llm.calls[0]
    assert first["system"].startswith("TASK: research")
    assert "at most\n   {} pages".format(cfg.max_fetches) in first["system"] \
        or "{} pages".format(cfg.max_fetches) in first["system"]
    user = first["messages"][0]["text"]
    assert "Company: Acme Analytics" in user
    assert "Website (if known): (unknown)" in user
    assert "Example Hackathon" in user and "Surat, Gujarat, India" in user
    assert [t["name"] for t in first["tools"]] == ["web_search", "fetch_page"]


def test_research_prospect_without_sources_has_zero_confidence(llm, cfg, db, prospect,
                                                                event):
    summary, excerpts, run = research.research_prospect(
        llm, cfg, db, prospect, event, frozen=[])
    assert excerpts == []
    assert summary.confidence == 0.0
    assert summary.sources_used == []
    assert summary.thin
    assert db.one("SELECT COUNT(*) n FROM research")["n"] == 0


def test_research_prospect_survives_budget_exhaustion(llm, cfg, db, prospect, event):
    cfg.max_steps = 2                     # search, one fetch, then the loop ends
    summary, excerpts, run = research.research_prospect(
        llm, cfg, db, prospect, event, frozen=SOURCES)
    assert run.hit_budget
    assert len(excerpts) == 1
    assert summary.confidence <= 0.5
    assert summary.sources_used == [excerpts[0].url]     # fallback: what we fetched
    assert summary.company_domain == "acme-analytics.example.com"


def test_research_prospect_drops_unfetched_sources_and_tolerates_junk(cfg, db, prospect,
                                                                       event):
    scripted = [
        LLMResponse(tool_calls=[ToolCall("fetch_page", {"url": SOURCES[1].url}, id="a")]),
        LLMResponse(text=json.dumps({
            "one_liner": " Acme builds a workbench. ",
            "hook": "free tier for learners",
            "company_domain": "https://www.Acme-Analytics.example.com/about",
            "sources_used": ["https://never-fetched.example.com/", SOURCES[1].url + "/"],
            "confidence": "high",                        # not a number
        })),
    ]
    llm = FakeLLM(cfg, scripted=scripted)
    summary, excerpts, run = research.research_prospect(
        llm, cfg, db, prospect, event, frozen=SOURCES)
    assert summary.sources_used == [SOURCES[1].url]
    assert summary.one_liner == "Acme builds a workbench."
    assert summary.confidence == 0.0                     # unparseable -> 0, not a crash
    assert summary.company_domain == "acme-analytics.example.com"
    assert summary.raw["hook"] == "free tier for learners"


def test_research_prospect_tolerates_non_json_answer(cfg, db, prospect, event):
    llm = FakeLLM(cfg, scripted=[LLMResponse(text="I could not find anything.")])
    summary, excerpts, run = research.research_prospect(
        llm, cfg, db, prospect, event, frozen=SOURCES)
    assert excerpts == [] and summary.confidence == 0.0 and summary.hook == ""
    assert summary.company_domain == ""                  # no website, nothing fetched


def test_research_prospect_prefers_company_site_for_domain(cfg, db, prospect, event):
    frozen = [
        Excerpt(url="https://en.wikipedia.org/wiki/Acme", title="Acme - Wikipedia",
                text="Acme Analytics is a company. " * 10),
        Excerpt(url="https://www.acme-analytics.example.com/about", title="About",
                text="Acme Analytics builds tools for students. " * 10),
    ]
    scripted = [
        LLMResponse(tool_calls=[ToolCall("fetch_page", {"url": frozen[0].url}, id="a"),
                                ToolCall("fetch_page", {"url": frozen[1].url}, id="b")]),
        LLMResponse(text=json.dumps({"hook": "h", "confidence": 0.7,
                                     "sources_used": [frozen[0].url, frozen[1].url]})),
    ]
    summary, excerpts, run = research.research_prospect(
        FakeLLM(cfg, scripted=scripted), cfg, db, prospect, event, frozen=frozen)
    assert summary.company_domain == "acme-analytics.example.com"
    assert summary.confidence == 0.7
    assert summary.sources_used == [frozen[0].url, frozen[1].url]
