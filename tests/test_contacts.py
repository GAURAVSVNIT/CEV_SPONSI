"""Contact discovery scoring.

Every case in ACCEPT and REJECT is a real address returned by a live sweep of
twenty companies on the ancestor project. One of them was worth having; the
rest are the reason this filter fails closed. Loose scoring surfaced another
charity's sponsorship inbox, two employees' personal addresses off a Devpost
page, and a support address lifted from a complaints site -- one usable result
out of twenty is not worth three ways to embarrass yourself.
"""

from __future__ import annotations

import pytest

from sponsy import contacts
from sponsy.contacts import score_email
from sponsy.models import Prospect

ACCEPT = [
    # the one good find: role mailbox, their domain, their own forum
    ("sponsorships@vercel.com", "Vercel",
     "https://community.vercel.com/t/looking-for-sponsorship-for-a-hackathon/10172"),
    ("community@postman.com", "Postman", "https://www.postman.com/community/"),
    ("devrel@groq.com", "Groq", "https://groq.com/community"),
    ("hackathons@replit.com", "Replit", "https://replit.com/site/events"),
]

REJECT = [
    # another organisation's sponsorship inbox, returned when researching Groq
    ("sponsorship@girldevelopit.com", "Groq",
     "https://girldevelopit.com/hackathon-sponsorship"),
    # real people, scraped off a hackathon listing
    ("sandipan@polygon.technology", "Polygon", "https://buidlit.devpost.com"),
    ("muskan@polygon.technology", "Polygon", "https://buidlit.devpost.com"),
    # lifted from a complaints site
    ("hr@zerodha.com", "Zerodha",
     "https://zerodha.pissedconsumer.com/customer-service.html"),
    ("fightstocktips@zerodha.com", "Zerodha",
     "https://zerodha.pissedconsumer.com/customer-service.html"),
    # somebody's typo of "security"
    ("secuty@huggingface.co", "Hugging Face",
     "https://discuss.huggingface.co/t/how-can-i-contact-the-team/75427"),
    # wrong department
    ("pr@freshworks.com", "Freshworks", "https://www.freshworks.com/pressrelease/x"),
    ("sales@zohocorp.com", "Zoho", "https://www.zoho.com/mail/contact.html"),
    ("support@zohomail.com", "Zoho", "https://www.zoho.com/mail/contact.html"),
    ("careers@postman.com", "Postman", "https://www.postman.com/company/careers/"),
    # never write to these
    ("noreply@vercel.com", "Vercel", "https://vercel.com"),
    ("privacy@vercel.com", "Vercel", "https://vercel.com"),
    # free mail and placeholders
    ("vercelsponsors@gmail.com", "Vercel", "https://vercel.com"),
    ("hello@example.com", "Vercel", "https://vercel.com"),
    # not an address at all
    ("logo@2x.png", "Vercel", "https://vercel.com"),
]


@pytest.mark.parametrize("address,company,source", ACCEPT)
def test_accepted(address, company, source):
    score, why = score_email(address, company, source)
    assert score > 0, "{} was rejected: {}".format(address, why)


@pytest.mark.parametrize("address,company,source", REJECT)
def test_rejected(address, company, source):
    score, why = score_email(address, company, source)
    assert score <= 0, "{} was accepted ({}): {}".format(address, score, why)


def test_a_partnership_mailbox_outranks_a_general_one():
    good, _ = score_email("sponsorships@vercel.com", "Vercel",
                          "https://community.vercel.com/t/sponsorship")
    generic, _ = score_email("hello@vercel.com", "Vercel",
                             "https://community.vercel.com/t/sponsorship")
    assert good > generic


def test_their_own_site_outranks_a_third_party_page():
    own, _ = score_email("sponsors@acme.com", "Acme", "https://acme.com/sponsors")
    third, _ = score_email("sponsors@acme.com", "Acme", "https://someblog.com/x")
    assert own > third


def test_a_wrong_domain_is_never_a_near_miss():
    """It must be excluded outright, not merely ranked lower -- otherwise it
    surfaces as the top candidate whenever nothing better exists."""
    score, why = score_email("sponsorship@someoneelse.org", "Groq",
                             "https://someoneelse.org/sponsor")
    assert score == -99 and "own domain" in why


def test_a_known_website_teaches_it_the_real_domain():
    """The company name alone cannot tell that community@fb.com is Meta's."""
    assert score_email("community@fb.com", "Meta", "https://fb.com/community")[0] <= 0
    assert score_email("community@fb.com", "Meta", "https://fb.com/community",
                       website="https://fb.com")[0] > 0


def test_a_subdomain_still_counts_as_theirs():
    assert score_email("sponsors@developer.acme.com", "Acme",
                       "https://developer.acme.com/sponsors")[0] > 0


def test_obfuscated_addresses_are_recovered():
    found = contacts._addresses("write to sponsors [at] acme [dot] com please")
    assert "sponsors@acme.com" in found


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

PAGES = {
    "https://acme.com/sponsorship": (
        "Acme sponsorship",
        "We sponsor student hackathons every year. Sponsor enquiries go to "
        "sponsorships@acme.com. Our sponsor programme is open to any sponsor "
        "who wants to reach developers. Press: pr@acme.com"),
    "https://scraper.example/leadiq/acme": (
        "Acme contacts",
        "Verified emails: ceo@acme.com, ramesh@acme.com, sponsors@acme.com"),
}


@pytest.fixture
def sweep(monkeypatch):
    """Search and fetch are patched on the module under test, so no network."""
    monkeypatch.setattr(contacts, "search", lambda cfg, q, k=4: [
        {"url": url, "title": "", "snippet": ""} for url in PAGES])

    def fake_fetch(cfg, url):
        if url not in PAGES:
            raise RuntimeError("404")
        return PAGES[url]

    monkeypatch.setattr(contacts, "fetch_url", fake_fetch)


def _prospect(**kwargs):
    return Prospect(id="P01", company="Acme", email="", **kwargs)


def test_the_sweep_finds_the_role_mailbox_and_the_page_it_was_on(cfg, sweep):
    finding = contacts.find_for(cfg, _prospect())
    assert finding.best.email == "sponsorships@acme.com"
    assert finding.best.source_url == "https://acme.com/sponsorship"
    assert "sponsorships@acme.com" in [c.email for c in finding.candidates]


def test_the_sweep_records_where_to_apply(cfg, sweep):
    """Most dev-tool companies route sponsorship through a form, not an inbox;
    the page is a useful answer even when no address is found."""
    finding = contacts.find_for(cfg, _prospect())
    assert "https://acme.com/sponsorship" in finding.form_urls


def test_the_sweep_drops_people_and_departments(cfg, sweep):
    emails = [c.email for c in contacts.find_for(cfg, _prospect()).candidates]
    assert "ramesh@acme.com" not in emails
    assert "ceo@acme.com" not in emails
    assert "pr@acme.com" not in emails


def test_an_address_only_a_scraper_site_knows_is_not_used(cfg, monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda cfg, q, k=4: [
        {"url": "https://scraper.example/leadiq/acme"}])
    monkeypatch.setattr(contacts, "fetch_url",
                        lambda cfg, url: PAGES["https://scraper.example/leadiq/acme"])
    assert contacts.find_for(cfg, _prospect()).candidates == []


def test_a_dead_page_does_not_stop_the_sweep(cfg, monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda cfg, q, k=4: [
        {"url": "https://dead.example/x"}, {"url": "https://acme.com/sponsorship"}])

    def fake_fetch(cfg, url):
        if "dead" in url:
            raise RuntimeError("connection reset")
        return PAGES["https://acme.com/sponsorship"]

    monkeypatch.setattr(contacts, "fetch_url", fake_fetch)
    assert contacts.find_for(cfg, _prospect()).best.email == "sponsorships@acme.com"


def test_the_known_website_is_read_when_search_returns_nothing(cfg, monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda cfg, q, k=4: [])
    monkeypatch.setattr(contacts, "fetch_url",
                        lambda cfg, url: PAGES["https://acme.com/sponsorship"])
    finding = contacts.find_for(cfg, _prospect(website="https://acme.com/sponsorship"))
    assert finding.best.email == "sponsorships@acme.com"


def test_nothing_found_is_an_empty_finding_not_a_crash(cfg, monkeypatch):
    monkeypatch.setattr(contacts, "search", lambda cfg, q, k=4: [])
    finding = contacts.find_for(cfg, _prospect())
    assert finding.candidates == [] and finding.best is None


# ---------------------------------------------------------------------------
# short company names: the near-miss domain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("address,company", [
    # Both of these were written into the real prospect list by a live sweep
    # before the matcher was tightened. miracare.com is a fertility tracker;
    # avadhtech.com is an engineering college in UP. Neither is the DotSlash
    # sponsor whose name they start with.
    ("partnership@miracare.com", "Mira"),
    ("info@avadhtech.com", "Avadh"),
    ("contact@wolframscience.com", "Wolfram"),
    ("hello@postmanager.com", "Postman"),
    ("team@groqify.io", "Groq"),
])
def test_a_domain_that_merely_starts_with_the_name_is_refused(address, company):
    score, why = score_email(address, company, "https://example.com/sponsor")
    assert score == -99, "{} was accepted for {}".format(address, company)
    assert "own domain" in why


@pytest.mark.parametrize("address,company", [
    ("sponsors@acme.io", "Acme Analytics"),        # domain is a shortening
    ("sponsors@acmecorp.com", "Acme"),             # a safe corporate suffix
    ("sponsors@acmeindia.com", "Acme"),
    ("community@devfolio.co", "Devfolio"),         # exact
])
def test_a_genuine_shortening_or_suffix_is_still_accepted(address, company):
    score, why = score_email(address, company, "https://example.com/sponsor")
    assert score > 0, "{} was rejected for {}: {}".format(address, company, why)
