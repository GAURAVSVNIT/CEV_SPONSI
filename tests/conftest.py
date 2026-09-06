"""Shared fixtures. Everything here is offline: FakeLLM, temp SQLite, console
notifier, frozen sources. No test in this suite touches the network."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sponsy import fakes  # noqa: E402, F401  (registers the fake handlers)
from sponsy import store  # noqa: E402
from sponsy.config import Config, Event, Tier, Voice  # noqa: E402
from sponsy.llm import FakeLLM  # noqa: E402
from sponsy.models import Excerpt  # noqa: E402
from sponsy.state import Database  # noqa: E402
from sponsy.telegram import ConsoleNotifier  # noqa: E402

TODAY = date(2026, 9, 5)

SOURCES = [
    Excerpt(
        url="https://acme-analytics.example.com/students",
        title="Acme Analytics for students",
        text=("Acme Analytics runs a student programme that gives free licences "
              "to college societies and coding clubs. The programme started in "
              "2023 and now covers more than 40 campuses. Student teams use the "
              "analytics workbench to build dashboards for hackathon projects, "
              "and the company has sponsored several college hackathons with "
              "challenge prizes for the best use of its API. Applications for "
              "campus partnerships are reviewed by the developer relations team.")),
    Excerpt(
        url="https://acme-analytics.example.com/about",
        title="About Acme Analytics",
        text=("Acme Analytics builds a data analytics workbench for product "
              "teams. The company was founded in Bengaluru and employs around "
              "120 people. Recently it launched a free tier for learners and a "
              "certification track that college students can complete online. "
              "The team writes regularly about data engineering on its blog, "
              "which is edited by Smit Patel.")),
]


@pytest.fixture
def cfg(tmp_path) -> Config:
    conf = Config.load(offline=True, dry_run=True, db_path=tmp_path / "t.db",
                       outbox=tmp_path / "outbox", event_path=tmp_path / "event.yml",
                       voice_path=tmp_path / "voice.yml",
                       # Point at a file that does not exist, so a real
                       # brochure sitting in data/ never changes a test.
                       brochure=tmp_path / "no-brochure.pdf")
    # Config.load() reads the real .env, and on a machine that is actually set
    # up that means live keys and a real From address leaking into the suite.
    # Blank every credential here: the tests that exercise loading do it with
    # a monkeypatched environment instead.
    # (Hosts are not credentials and several tests rely on their defaults.)
    for field in ("gemini_api_key", "openai_api_key", "anthropic_api_key",
                  "tavily_api_key", "brave_api_key", "telegram_bot_token",
                  "smtp_user", "smtp_password", "smtp_from",
                  "imap_user", "imap_password", "gmail_sender",
                  "model", "model_cheap"):
        setattr(conf, field, "")
    conf.gemini_api_keys = []
    conf.send_only_to = []
    conf.smtp_host = "smtp.gmail.com"
    conf.imap_host = "imap.gmail.com"
    conf.telegram_chat_id = "12345"
    conf.min_interval_s = 0
    conf.max_fetches = 4
    conf.grounding_mode = "hybrid"
    conf.humanize, conf.humanize_strict = True, False
    conf.allow_send = True
    return conf


@pytest.fixture
def db(cfg) -> Database:
    database = Database(cfg.db_path)
    store.ensure_schema(database)
    yield database
    database.close()


@pytest.fixture
def event() -> Event:
    return Event(
        event_name="Example Hackathon", date="2026-10-17",
        summary="A one-day case competition run by the finance club.",
        colleges="12 colleges in Gujarat", city="Surat", region="Gujarat",
        country="India", kind="case competition", currency="Rs",
        tiers=[Tier("Title", 50000, ["stage backdrop", "opening address"], "Rs"),
               Tier("Gold", 20000, ["stage backdrop", "stall on finals day"], "Rs"),
               Tier("Associate", 8000, ["logo on certificates"], "Rs")],
        sender_name="Rudraksh", sender_role="Sponsorship Lead", club="ACM SVNIT",
        reply_to="club@example.org", phone="+91 90000 00000",
        website="https://example.org", expected_participants=200,
        track_record=["Last year drew 200 participants from 12 colleges"],
        registrations_so_far=0, confirmed_sponsors=[])


@pytest.fixture
def voice(event) -> Voice:
    return Voice(
        sender_name="Rudraksh", sender_role="Sponsorship Lead", club="ACM SVNIT",
        greeting="Hi {first},", signoff="Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT",
        # A proposal, not a cold one-liner -- the same ceiling data/voice.yml
        # uses, so the suite measures what production produces.
        max_words=240,
        tone_notes=["The first paragraph is about them and why we wrote.",
                    "Suggest a tier, never insist on one.",
                    "Contractions are fine."],
        phrases_i_use=["happy to send more detail", "whichever fits you best"],
        phrases_i_avoid=["I hope this email finds you well", "synergy"],
        sample_emails=["Hi Priya,\n\nQuick one. We're running the case comp again "
                       "in October and your team judged last year. Up for it "
                       "again?\n\nThanks,\nRudraksh"])


@pytest.fixture
def llm(cfg) -> FakeLLM:
    return FakeLLM(cfg)


@pytest.fixture
def notifier(tmp_path) -> ConsoleNotifier:
    return ConsoleNotifier(tmp_path / "notifications.log")


@pytest.fixture
def prospect(db):
    db.execute(
        "INSERT INTO prospects (id, company, contact_name, email, segment, status, "
        "notes, source, created_at) VALUES ('P01','Acme Analytics','Devi Sharma',"
        "'devi@example.com','saas','new','student programme','csv',datetime('now'))")
    return store.get(db, "P01")


@pytest.fixture
def run_job(cfg, db, notifier, llm, event, voice):
    """Run the daily job offline against the frozen SOURCES for P01."""
    from sponsy.job import SponsorshipJob

    def _run(today: date = TODAY, limit: int = 5, frozen=None, **kwargs):
        job = SponsorshipJob(cfg, db, notifier, llm=llm, today=today, force=True,
                             event=event, voice=voice, limit=limit,
                             frozen=frozen if frozen is not None else {"P01": SOURCES},
                             poll_replies=False, **kwargs)
        return job.run()
    return _run
