"""Finding prospects by reading who already sponsors events like ours.

A company on four student-hackathon sponsor pages has demonstrated that it
sponsors student hackathons. That is evidence. "Feels like a good fit" is a
guess, and guessing is the thing this project refuses to do everywhere else.
"""

from __future__ import annotations

import pytest

from sponsy import prospecting, store
from sponsy.llm import LLMError

PAGES = {
    "https://hack1.example/sponsors": (
        "DotSlash 9.0 sponsors",
        "Sponsored by Postman, Devfolio, Groq. Organised by ACM SVNIT Surat."),
    "https://hack2.example/partners": (
        "Innovate 2026 partners",
        "Sponsored by Postman, Zoho. Powered by Devfolio."),
    "https://blog.example/unrelated": (
        "Ten tips", "A blog post about nothing in particular."),
}


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(prospecting, "search", lambda cfg, q, k=4: [
        {"url": url, "title": "", "snippet": ""} for url in PAGES])

    def fake_fetch(cfg, url):
        if url not in PAGES:
            raise RuntimeError("404")
        return PAGES[url]

    monkeypatch.setattr(prospecting, "fetch_url", fake_fetch)


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Postman", "Postman"),
    ("  Postman  ", "Postman"),
    ("T- Mobile", "T-Mobile"),
    ("Devfolio (platform partner)", "Devfolio"),
    ("Breaking the Glass, powered", "Breaking the Glass"),
    ("Acme, sponsored by", "Acme"),
    ("- Postman", "Postman"),
    # the label the extractor read off the page, on the front of the name
    ("Sponsored by Postman", "Postman"),
    ("Powered by Devfolio", "Devfolio"),
    ("Presented by: Zoho", "Zoho"),
    ("In partnership with Groq", "Groq"),
    ("Proudly supported by Twilio", "Twilio"),
])
def test_clean_name_keeps_the_company(raw, expected):
    assert prospecting.clean_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "  ", ".xyz", "a", "12345", "sponsors", "Our Sponsors", "gold",
    "coming soon", "one two three four five six seven",
])
def test_clean_name_rejects_debris(raw):
    assert prospecting.clean_name(raw) == ""


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def test_the_queries_are_anchored_on_the_event(event):
    queries = prospecting.default_queries(event)
    joined = " | ".join(queries).lower()
    assert "gujarat" in joined
    assert "case competition" in joined
    assert "india" in joined
    assert event.event_name.lower() in joined


def test_there_are_no_duplicate_queries(event):
    queries = prospecting.default_queries(event)
    assert len(queries) == len(set(q.lower() for q in queries))


def test_a_sparse_event_still_produces_usable_queries(event):
    event.city = event.region = event.country = ""
    queries = prospecting.default_queries(event)
    assert queries and all(q.strip() for q in queries)
    assert not any("  " in q for q in queries)


def test_a_named_campus_in_the_audience_line_becomes_a_query(event):
    event.colleges = "Students from SVNIT Surat and nearby colleges"
    assert any("SVNIT" in q for q in prospecting.default_queries(event))


def test_the_host_club_is_not_treated_as_a_sponsor(event):
    words = prospecting.host_words(event)
    assert "acm" in words and "svnit" in words


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def test_a_company_on_two_pages_outranks_one_on_a_single_page(cfg, db, llm, event,
                                                              web):
    leads = prospecting.discover(cfg, llm, event, db=db)
    by_name = {lead.company: lead for lead in leads}
    assert "Postman" in by_name
    assert by_name["Postman"].strength == 2
    assert leads[0].company == "Postman", "strongest evidence should sort first"


def test_the_platform_the_event_was_listed_on_is_not_a_prospect(cfg, db, llm, event,
                                                                web):
    names = {lead.company.lower() for lead in prospecting.discover(cfg, llm, event,
                                                                   db=db)}
    assert "devfolio" not in names


def test_the_host_club_is_not_a_prospect(cfg, db, llm, event, web):
    names = {lead.company.lower() for lead in prospecting.discover(cfg, llm, event,
                                                                   db=db)}
    assert not any("acm" in n or "svnit" in n for n in names)


def test_each_lead_carries_the_page_it_was_found_on(cfg, db, llm, event, web):
    for lead in prospecting.discover(cfg, llm, event, db=db):
        assert lead.sources and lead.sources[0].startswith("http")


def test_a_page_that_is_not_about_sponsors_contributes_nothing(cfg, db, llm, event,
                                                               monkeypatch):
    monkeypatch.setattr(prospecting, "search", lambda cfg, q, k=4: [
        {"url": "https://blog.example/unrelated"}])
    monkeypatch.setattr(prospecting, "fetch_url",
                        lambda cfg, url: PAGES["https://blog.example/unrelated"])
    assert prospecting.discover(cfg, llm, event, db=db) == []


def test_a_dead_page_does_not_stop_the_sweep(cfg, db, llm, event, monkeypatch):
    monkeypatch.setattr(prospecting, "search", lambda cfg, q, k=4: [
        {"url": "https://dead.example/x"}, {"url": "https://hack1.example/sponsors"}])

    def fake_fetch(cfg, url):
        if "dead" in url:
            raise RuntimeError("connection reset")
        return PAGES["https://hack1.example/sponsors"]

    monkeypatch.setattr(prospecting, "fetch_url", fake_fetch)
    assert prospecting.discover(cfg, llm, event, db=db)


def test_running_dry_on_quota_is_not_reported_as_zero_results(cfg, db, event, web):
    """"0 companies found" would be a lie: we never got to look."""
    from sponsy.llm import QuotaExhausted

    class Dry:
        name = "dry"

        def generate(self, **kwargs):
            raise QuotaExhausted("daily cap")

    with pytest.raises(QuotaExhausted):
        prospecting.discover(cfg, Dry(), event, db=db)


def test_an_ordinary_model_error_only_costs_that_page(cfg, db, event, web):
    class Flaky:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            raise LLMError("hiccup")

    assert prospecting.discover(cfg, Flaky(), event, db=db) == []


def test_an_explicit_query_is_searched_first(cfg, db, llm, event, monkeypatch):
    """`--query` runs in full before the event's own queries, so the pages it
    finds get the page budget rather than being crowded out by the defaults."""
    seen = []
    monkeypatch.setattr(prospecting, "search",
                        lambda cfg, q, k=4: seen.append(q) or [])
    prospecting.discover(cfg, llm, event, queries=["only this one"], db=db)
    assert seen[0] == "only this one"
    assert set(seen[1:]) == set(prospecting.default_queries(event))


# ---------------------------------------------------------------------------
# collision with the list you already have
# ---------------------------------------------------------------------------

def test_a_company_already_on_the_list_is_known(db):
    store.add_prospect(db, "Postman", email="community@postman.com")
    assert prospecting.already_known(db, "Postman Inc.")
    assert prospecting.already_known(db, "postman")
    assert not prospecting.already_known(db, "Groq")
