"""config.py: provider resolution, pricing, mail/replies routing, Event, Voice.

Nothing here reads the real .env: Config.load is exercised with load_dotenv
monkeypatched to a no-op, so a key on the developer's machine cannot change a
test's answer.
"""

from __future__ import annotations

import os

import pytest

from sponsy import config
from sponsy.config import (DATA, DEFAULT_MODELS, DEFAULT_PRICE, Config, Event, Tier,
                           Voice, load_dotenv)


def _cfg(tmp_path, **kw) -> Config:
    """A Config with every file path pointing into tmp, so what exists on the
    developer's disk (a real gmail token, say) cannot leak into a test."""
    base = dict(db_path=tmp_path / "t.db", outbox=tmp_path / "out",
                gmail_credentials=tmp_path / "creds.json",
                gmail_token=tmp_path / "token.json")
    base.update(kw)
    return Config(**base)


# ---------------------------------------------------------------------------
# model provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fields,expected", [
    ({}, "fake"),
    ({"gemini_api_key": "g"}, "gemini"),
    ({"gemini_api_keys": ["g2"]}, "gemini"),
    ({"openai_api_key": "o"}, "openai"),
    ({"anthropic_api_key": "a"}, "anthropic"),
    ({"openai_api_key": "o", "anthropic_api_key": "a"}, "openai"),
    ({"gemini_api_key": "g", "openai_api_key": "o", "anthropic_api_key": "a"},
     "gemini"),
])
def test_auto_provider_resolution(tmp_path, fields, expected):
    assert _cfg(tmp_path, **fields).resolved_llm_provider() == expected


def test_explicit_provider_beats_the_keys(tmp_path):
    cfg = _cfg(tmp_path, llm_provider="anthropic", gemini_api_key="g")
    assert cfg.resolved_llm_provider() == "anthropic"
    assert _cfg(tmp_path, llm_provider="fake", openai_api_key="o") \
        .resolved_llm_provider() == "fake"


@pytest.mark.parametrize("provider", ["gemini", "openai", "anthropic", "fake"])
def test_resolved_models_default_per_provider(tmp_path, provider):
    cfg = _cfg(tmp_path, llm_provider=provider)
    assert cfg.resolved_models() == DEFAULT_MODELS[provider]


def test_llm_model_overrides_the_default(tmp_path):
    cfg = _cfg(tmp_path, llm_provider="openai", model="gpt-5")
    assert cfg.resolved_models() == ("gpt-5", "gpt-5-nano")
    cfg = _cfg(tmp_path, llm_provider="gemini", model_cheap="gemini-2.5-flash-lite")
    assert cfg.resolved_models() == ("gemini-3.5-flash", "gemini-2.5-flash-lite")
    cfg = _cfg(tmp_path, llm_provider="anthropic", model="claude-sonnet-5",
               model_cheap="claude-sonnet-5")
    assert cfg.resolved_models() == ("claude-sonnet-5", "claude-sonnet-5")


def test_default_models_and_prices_agree():
    """Every default model must have a price row, or costs silently fall back."""
    for default, cheap in DEFAULT_MODELS.values():
        for name in (default, cheap):
            assert any(name.startswith(p) for p in config.PRICES_USD_PER_MTOK), name


def test_price_picks_the_longest_prefix(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.price("gpt-5-mini") == (0.25, 2.00)
    assert cfg.price("gpt-5") == (1.25, 10.00)
    assert cfg.price("gpt-5-mini-2026-03-01") == (0.25, 2.00)
    assert cfg.price("gpt-5-2026-03-01") == (1.25, 10.00)
    assert cfg.price("gemini-3.5-flash-lite-preview") == (0.10, 0.40)
    assert cfg.price("gemini-3.5-flash") == (0.30, 2.50)
    assert cfg.price("fake") == (0.0, 0.0)


def test_unknown_model_gets_the_default_price(tmp_path):
    assert _cfg(tmp_path).price("mystery-model-9") == DEFAULT_PRICE
    assert _cfg(tmp_path).price("") == DEFAULT_PRICE


def test_api_keys_are_ordered_and_deduplicated(tmp_path):
    cfg = _cfg(tmp_path, gemini_api_key="A", gemini_api_keys=["A", " B ", "", "C"])
    assert cfg.api_keys == ["A", "B", "C"]
    assert _cfg(tmp_path).api_keys == []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_provider_auto_rules(tmp_path):
    assert _cfg(tmp_path).resolved_search_provider() == "ddg"
    assert _cfg(tmp_path, brave_api_key="b").resolved_search_provider() == "brave"
    assert _cfg(tmp_path, brave_api_key="b", tavily_api_key="t") \
        .resolved_search_provider() == "tavily"
    assert _cfg(tmp_path, search_provider="none", tavily_api_key="t") \
        .resolved_search_provider() == "none"


# ---------------------------------------------------------------------------
# mail and replies
# ---------------------------------------------------------------------------

def test_mail_allow_send_off_means_manual(tmp_path):
    (tmp_path / "token.json").write_text("{}")
    cfg = _cfg(tmp_path, allow_send=False, smtp_user="u", smtp_password="p",
               mail_provider="gmail_api")
    assert cfg.resolved_mail_provider() == "manual"


def test_mail_gmail_token_present(tmp_path):
    (tmp_path / "token.json").write_text("{}")
    cfg = _cfg(tmp_path, smtp_user="u", smtp_password="p")
    assert cfg.gmail_api_enabled
    assert cfg.resolved_mail_provider() == "gmail_api", "gmail beats smtp"


def test_mail_gmail_credentials_alone_are_enough_to_pick_gmail(tmp_path):
    """The OAuth client file means the user chose Gmail; the token comes from
    `gmail-auth`, and doctor says so."""
    (tmp_path / "creds.json").write_text("{}")
    assert _cfg(tmp_path).resolved_mail_provider() == "gmail_api"


def test_mail_smtp_creds(tmp_path):
    cfg = _cfg(tmp_path, smtp_user="club@example.org", smtp_password="app-pass")
    assert cfg.smtp_enabled
    assert cfg.resolved_mail_provider() == "smtp"


def test_smtp_is_off_without_a_password(tmp_path):
    cfg = _cfg(tmp_path, smtp_user="club@example.org", smtp_password="")
    assert not cfg.smtp_enabled, "announcing SMTP with no password loses emails"
    assert cfg.resolved_mail_provider() == "manual"


def test_mail_nothing_configured_means_manual(tmp_path):
    cfg = _cfg(tmp_path)
    assert not cfg.gmail_api_enabled
    assert cfg.resolved_mail_provider() == "manual"


def test_mail_explicit_provider_is_honoured(tmp_path):
    (tmp_path / "token.json").write_text("{}")
    cfg = _cfg(tmp_path, mail_provider="smtp", smtp_user="u", smtp_password="p")
    assert cfg.resolved_mail_provider() == "smtp"
    assert _cfg(tmp_path, mail_provider="manual", smtp_user="u", smtp_password="p") \
        .resolved_mail_provider() == "manual"
    # explicit but unusable: resolution reports the choice; doctor reports the gap
    assert _cfg(tmp_path, mail_provider="gmail_api").resolved_mail_provider() \
        == "gmail_api"


def test_replies_auto_rules(tmp_path):
    assert _cfg(tmp_path).resolved_replies_provider() == "none"
    imap = dict(imap_user="u", imap_password="p")
    assert _cfg(tmp_path, **imap).resolved_replies_provider() == "imap"
    assert _cfg(tmp_path, imap_user="u").resolved_replies_provider() == "none", \
        "IMAP without a password is not configured"
    (tmp_path / "token.json").write_text("{}")
    assert _cfg(tmp_path, **imap).resolved_replies_provider() == "gmail_api"
    assert _cfg(tmp_path, mail_provider="gmail_api").resolved_replies_provider() \
        == "gmail_api"
    # mail explicitly on smtp: the token is not in play for replies either
    assert _cfg(tmp_path, mail_provider="smtp", **imap).resolved_replies_provider() \
        == "imap"
    assert _cfg(tmp_path, mail_provider="smtp").resolved_replies_provider() == "none"


def test_replies_credentials_file_alone_is_not_enough(tmp_path):
    """Polling needs an authorised token, not just the OAuth client."""
    (tmp_path / "creds.json").write_text("{}")
    assert _cfg(tmp_path).resolved_replies_provider() == "none"


def test_replies_explicit_provider_is_honoured(tmp_path):
    (tmp_path / "token.json").write_text("{}")
    assert _cfg(tmp_path, replies_provider="none").resolved_replies_provider() == "none"
    assert _cfg(tmp_path, replies_provider="imap").resolved_replies_provider() == "imap"


def test_channel_flags(tmp_path):
    assert not _cfg(tmp_path).telegram_enabled
    assert not _cfg(tmp_path, telegram_bot_token="t").telegram_enabled
    assert _cfg(tmp_path, telegram_bot_token="t", telegram_chat_id="1").telegram_enabled
    assert _cfg(tmp_path, imap_user="u", imap_password="p").imap_enabled
    assert not _cfg(tmp_path, imap_host="", imap_user="u", imap_password="p").imap_enabled


# ---------------------------------------------------------------------------
# Config.load
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    """No .env, and none of the keys Config.load reads present in the env."""
    monkeypatch.setattr(config, "load_dotenv", lambda path=None: None)
    for key in ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_API_KEYS", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "LLM_MODEL", "LLM_MODEL_CHEAP", "SEARCH_PROVIDER",
                "TAVILY_API_KEY", "BRAVE_API_KEY", "MAIL_PROVIDER", "REPLIES_PROVIDER",
                "SMTP_USER", "SMTP_PASSWORD", "IMAP_USER", "IMAP_PASSWORD",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALLOW_SEND", "HUMANIZE",
                "HUMANIZE_STRICT", "GMAIL_TOKEN", "GMAIL_CREDENTIALS", "SPONSY_DB",
                "LLM_MAX_COST", "LLM_MIN_INTERVAL", "MAX_FETCHES"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_load_reads_the_environment(clean_env, tmp_path):
    clean_env.setenv("LLM_PROVIDER", "OpenAI")
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_env.setenv("GEMINI_API_KEYS", "k1, k2 ,,k1")
    clean_env.setenv("LLM_MAX_COST", "0.5")
    clean_env.setenv("MAX_FETCHES", "2")
    clean_env.setenv("HUMANIZE", "0")
    clean_env.setenv("HUMANIZE_STRICT", "yes")
    clean_env.setenv("ALLOW_SEND", "false")
    clean_env.setenv("GMAIL_TOKEN", "data/some-token.json")
    cfg = Config.load(db_path=tmp_path / "t.db", outbox=tmp_path / "out")
    assert cfg.llm_provider == "openai"
    assert cfg.resolved_llm_provider() == "openai"
    assert cfg.gemini_api_keys == ["k1", "k2", "k1"]
    assert cfg.api_keys == ["k1", "k2"]
    assert cfg.max_cost_usd == 0.5
    assert cfg.max_fetches == 2
    assert cfg.humanize is False and cfg.humanize_strict is True
    assert cfg.allow_send is False
    assert cfg.gmail_token == config.ROOT / "data" / "some-token.json", \
        "relative paths are anchored at the repo root"
    assert (tmp_path / "out").is_dir(), "the outbox is created up front"


def test_load_with_a_bad_llm_provider_raises(clean_env, tmp_path):
    clean_env.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Config.load(db_path=tmp_path / "t.db", outbox=tmp_path / "out")


@pytest.mark.parametrize("key,value", [
    ("SEARCH_PROVIDER", "google"), ("MAIL_PROVIDER", "sendgrid"),
    ("REPLIES_PROVIDER", "pop3"),
])
def test_load_with_other_bad_providers_raises(clean_env, tmp_path, key, value):
    clean_env.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        Config.load(db_path=tmp_path / "t.db", outbox=tmp_path / "out")


def test_bad_numbers_fall_back_to_defaults(clean_env, tmp_path):
    clean_env.setenv("LLM_MAX_COST", "lots")
    clean_env.setenv("MAX_FETCHES", "many")
    cfg = Config.load(db_path=tmp_path / "t.db", outbox=tmp_path / "out")
    assert cfg.max_cost_usd == 0.05
    assert cfg.max_fetches == 4


def test_offline_forces_every_channel_off(clean_env, tmp_path):
    clean_env.setenv("LLM_PROVIDER", "gemini")
    clean_env.setenv("GEMINI_API_KEY", "real-looking-key")
    clean_env.setenv("SEARCH_PROVIDER", "tavily")
    clean_env.setenv("TAVILY_API_KEY", "t")
    clean_env.setenv("MAIL_PROVIDER", "smtp")
    clean_env.setenv("SMTP_USER", "u")
    clean_env.setenv("SMTP_PASSWORD", "p")
    clean_env.setenv("REPLIES_PROVIDER", "imap")
    clean_env.setenv("IMAP_USER", "u")
    clean_env.setenv("IMAP_PASSWORD", "p")
    cfg = Config.load(offline=True, db_path=tmp_path / "t.db", outbox=tmp_path / "out")
    assert cfg.offline
    assert (cfg.resolved_llm_provider(), cfg.resolved_search_provider(),
            cfg.resolved_mail_provider(), cfg.resolved_replies_provider()) == (
        "fake", "none", "manual", "none")


def test_overrides_win_and_none_is_ignored(clean_env, tmp_path):
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    cfg = Config.load(llm_provider="fake", dry_run=None, db_path=tmp_path / "t.db",
                      outbox=tmp_path / "out")
    assert cfg.llm_provider == "fake"
    assert cfg.dry_run is False
    assert cfg.db_path == tmp_path / "t.db"


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

def test_load_dotenv_does_not_override_existing_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\n"
                   "SPONSY_TEST_KEPT=from_file\n"
                   "SPONSY_TEST_NEW = 'quoted value'\n"
                   "SPONSY_TEST_DQ=\"dq\"\n"
                   "not a pair\n"
                   "SPONSY_TEST_EQ=a=b\n", encoding="utf-8")
    monkeypatch.setenv("SPONSY_TEST_KEPT", "from_env")
    for name in ("SPONSY_TEST_NEW", "SPONSY_TEST_DQ", "SPONSY_TEST_EQ"):
        # setenv then delenv, so monkeypatch restores "absent" at teardown
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)

    load_dotenv(env)

    assert os.environ["SPONSY_TEST_KEPT"] == "from_env"
    assert os.environ["SPONSY_TEST_NEW"] == "quoted value"
    assert os.environ["SPONSY_TEST_DQ"] == "dq"
    assert os.environ["SPONSY_TEST_EQ"] == "a=b"


def test_load_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

def test_event_example_parses():
    ev = Event.load(DATA / "event.example.yml")
    assert ev.event_name == "DotSlash 10.0"
    assert isinstance(ev.date, str) and "2027" in ev.date
    assert ev.tier_names == ["Alpha", "Beta", "Gamma", "Delta"]
    assert [t.amount for t in ev.tiers] == [750, 500, 250, 100]
    assert all(t.currency == "USD" for t in ev.tiers)
    assert all(isinstance(t, Tier) and t.deliverables for t in ev.tiers)
    assert ev.where() == "Surat, Gujarat, India"
    assert ev.kind == "hackathon"
    assert len(ev.track_record) == 8
    assert ev.confirmed_sponsors == []
    assert ev.registrations_so_far == 0
    assert ev.phone.startswith("+91")


def test_event_blocks(event):
    tiers = event.tier_block().splitlines()
    assert tiers[0] == "- Title (Rs 50,000): stage backdrop; opening address"
    assert len(tiers) == 3
    assert event.track_record_block() == "- Last year drew 200 participants from 12 colleges"
    assert Event(event_name="x", date="", colleges="", tiers=[], sender_name="",
                 sender_role="", club="", reply_to="").track_record_block() \
        == "(none recorded)"
    assert event.where() == "Surat, Gujarat, India"
    assert Event(event_name="x", date="", colleges="", tiers=[], sender_name="",
                 sender_role="", club="", reply_to="", city="Pune").where() == "Pune"


def test_event_load_tolerates_extra_keys_and_a_yaml_date(tmp_path):
    path = tmp_path / "event.yml"
    path.write_text("event_name: Fest\ndate: 2026-10-17\ncolleges: some\n"
                    "sender_name: A\nsender_role: B\nclub: C\nreply_to: a@b.c\n"
                    "city: null\nreference_notes: ignored\n"
                    "tiers:\n  - name: Gold\n    amount: 1000\n    currency: Rs\n"
                    "    deliverables: [logo]\n", encoding="utf-8")
    ev = Event.load(path)
    assert ev.date == "2026-10-17"
    assert ev.city == ""
    assert ev.tiers[0].currency == "Rs"
    assert ev.tiers[0].line() == "Gold (Rs 1,000): logo"


def test_event_load_missing_file_has_a_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="event.example.yml"):
        Event.load(tmp_path / "missing.yml")


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

def test_voice_falls_back_to_event_fields_and_synthesises_a_signoff(tmp_path, event):
    voice = Voice.load(tmp_path / "missing-voice.yml", event)
    assert voice.sender_name == "Rudraksh"
    assert voice.sender_role == "Sponsorship Lead"
    assert voice.club == "ACM SVNIT"
    assert voice.signoff == "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT"
    assert voice.greeting == "Hi {first},"
    assert voice.max_words == 240
    assert voice.sample_emails == []


def test_voice_file_wins_over_event_but_gaps_are_filled(tmp_path, event):
    path = tmp_path / "voice.yml"
    path.write_text("sender_name: Asha\nsignoff: |\n  Cheers,\n  Asha\n"
                    "max_words: 90\ntone_notes: one note only\n"
                    "phrases_i_use:\n  - quick one\n  - ''\n"
                    "sample_emails: null\nunknown_key: 1\n", encoding="utf-8")
    voice = Voice.load(path, event)
    assert voice.sender_name == "Asha"
    assert voice.sender_role == "Sponsorship Lead", "filled from the event"
    assert voice.signoff == "Cheers,\nAsha"
    assert voice.max_words == 90
    assert voice.tone_notes == ["one note only"], "a bare string becomes a list"
    assert voice.phrases_i_use == ["quick one"], "blank entries are dropped"
    assert voice.sample_emails == []


def test_voice_example_loads():
    voice = Voice.load(DATA / "voice.example.yml")
    assert voice.greeting == "Hi {first},"
    assert "I hope this email finds you well" in voice.phrases_i_avoid
    assert len(voice.sample_emails) == 1
    assert "Thanks," in voice.signoff


@pytest.mark.parametrize("contact,expected", [
    ("Devi Sharma", "Hi Devi,"),
    ("devi", "Hi devi,"),
    ("sponsorships@acme.example", "Hi team,"),
    ("", "Hi there,"),
    ("   ", "Hi there,"),
    ("DevRel team", "Hi DevRel team,"),
    ("Marketing", "Hi team,"),
    ("Community Team", "Hi Community Team,"),
])
def test_greeting_for(voice, contact, expected):
    assert voice.greeting_for(contact) == expected


def test_greeting_for_with_a_custom_template_and_a_broken_one(voice):
    voice.greeting = "Hello {first}"
    assert voice.greeting_for("Devi Sharma") == "Hello Devi"
    voice.greeting = "Dear {who},"
    assert voice.greeting_for("Devi Sharma") == "Hi Devi,", "a bad template degrades"


def test_voice_blocks(voice):
    style = voice.style_block()
    assert "Sender: Rudraksh, Sponsorship Lead, ACM SVNIT" in style
    assert "under {} words".format(voice.max_words) in style
    assert '"happy to send more detail"' in style
    assert '"synergy"' in style
    assert "Suggest a tier, never insist on one." in style
    examples = voice.examples_block()
    assert examples.startswith("Emails this person actually wrote")
    assert "--- example 1 ---" in examples
    assert "Hi Priya," in examples
    assert Voice().examples_block() == "(no sample emails from the sender were provided)"


def test_examples_block_caps_at_three(voice):
    voice.sample_emails = ["one", "two", "three", "four"]
    block = voice.examples_block()
    assert "--- example 3 ---" in block
    assert "--- example 4 ---" not in block


@pytest.mark.parametrize("contact,company,expected", [
    # no person at all: the company is what we do know
    ("", "DigitalOcean", "Hi DigitalOcean Team,"),
    ("", "Coding Blocks", "Hi Coding Blocks Team,"),
    # a role mailbox is not a person either
    ("sponsorships@vercel.com", "Vercel", "Hi Vercel Team,"),
    ("community", "Devfolio", "Hi Devfolio Team,"),
    # a real name still wins
    ("Devi Sharma", "Acme Analytics", "Hi Devi,"),
    # already says "team": leave the caller's wording alone
    ("Partnerships Team", "Acme", "Hi Partnerships Team,"),
    # nothing known: the old fallback
    ("", "", "Hi there,"),
])
def test_greeting_uses_the_company_when_there_is_no_person(voice, contact, company,
                                                           expected):
    """Most sponsorship inboxes are sponsorships@ with no name attached, and
    "Hi there," is how a mailshot opens. We always know the company."""
    assert voice.greeting_for(contact, company) == expected


def test_greeting_without_a_company_still_works(voice):
    """The company argument is optional: older callers must not break."""
    assert voice.greeting_for("Devi Sharma") == "Hi Devi,"
    assert voice.greeting_for("") == "Hi there,"
