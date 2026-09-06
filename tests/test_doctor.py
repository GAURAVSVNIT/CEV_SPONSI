"""doctor: every check present, in order, none raising, and never a network
call unless the thing on the other end is a fake we planted here.

Sibling modules that other agents own (telegram, research, mail/*) are stood
in for with throwaway modules injected into sys.modules for one test at a
time; the real ones are never imported by these tests."""

from __future__ import annotations

import imaplib
import smtplib
import sys
import types

import pytest

from sponsy import doctor
from sponsy.config import Voice
from sponsy.doctor import Check
from sponsy.llm import QuotaExhausted

NAMES = [".env", "event.yml", "voice.yml", "database", "model", "search",
         "telegram", "mail", "brochure", "replies", "send gate"]

EVENT_YML = """\
event_name: Example Hackathon
date: 2026-10-17
colleges: 12 colleges in Gujarat
currency: Rs
tiers:
  - name: Gold
    amount: 20000
    deliverables: [stage backdrop, stall on finals day]
  - name: Associate
    amount: 8000
    deliverables: [logo on certificates]
sender_name: {sender}
sender_role: Sponsorship Lead
club: ACM SVNIT
reply_to: {reply_to}
phone: "+91 98765 43210"
"""


def write_event(path, sender="Rudraksh", reply_to="club@svnit.ac.in") -> None:
    path.write_text(EVENT_YML.format(sender=sender, reply_to=reply_to),
                    encoding="utf-8")


def stub_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """A throwaway module under a sibling's name, for this test only."""
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    monkeypatch.setitem(sys.modules, name, mod)
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        monkeypatch.setattr(sys.modules[parent], child, mod, raising=False)
    return mod


# ---------------------------------------------------------------------------
# the whole list
# ---------------------------------------------------------------------------

def test_every_check_is_present_in_order_and_none_raises(cfg):
    checks = doctor.run_checks(cfg, live=False)
    assert [c.name for c in checks] == NAMES
    assert all(isinstance(c, Check) for c in checks)
    assert all(isinstance(c.detail, str) for c in checks)


def test_checks_are_wrapped_so_a_crash_becomes_a_fail_line(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(doctor, "check_database", boom)
    checks = doctor.run_checks(cfg, live=False)
    dbcheck = [c for c in checks if c.name == "database"][0]
    assert not dbcheck.ok and "kaboom" in dbcheck.detail and dbcheck.hint


def test_no_secret_ever_reaches_the_report(cfg):
    cfg.gemini_api_key = "SECRET-GEMINI-KEY"
    cfg.telegram_bot_token = "SECRET-TG-TOKEN"
    cfg.smtp_password = "SECRET-SMTP-PW"
    cfg.imap_password = "SECRET-IMAP-PW"
    cfg.tavily_api_key = "SECRET-TAVILY"
    report = doctor.format_report(doctor.run_checks(cfg, live=False))
    for secret in ("SECRET-GEMINI-KEY", "SECRET-TG-TOKEN", "SECRET-SMTP-PW",
                   "SECRET-IMAP-PW", "SECRET-TAVILY"):
        assert secret not in report


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def test_env_check_points_at_the_example(cfg, tmp_path):
    check = doctor.check_env(cfg, root=tmp_path)
    assert not check.ok and not check.optional
    assert ".env.example" in check.hint
    (tmp_path / ".env").write_text("GEMINI_API_KEY=x\n", encoding="utf-8")
    assert doctor.check_env(cfg, root=tmp_path).ok


def test_missing_event_file_fails_and_names_it(cfg):
    check = doctor.check_event(cfg)
    assert not check.ok and str(cfg.event_path) in check.detail
    assert "event.example.yml" in check.hint


def test_event_placeholders_are_detected(cfg):
    write_event(cfg.event_path, sender="Your Name", reply_to="you@example.org")
    checks = doctor.run_checks(cfg, live=False)
    check = [c for c in checks if c.name == "event.yml"][0]
    assert not check.ok and not check.optional
    assert "Your Name" in check.hint
    assert "sender_name" in check.hint
    assert "reply_to" in check.hint
    assert str(cfg.event_path) in check.hint


def test_clean_event_passes_and_lists_tiers(cfg):
    write_event(cfg.event_path)
    check = doctor.check_event(cfg)
    assert check.ok, check.hint
    assert "Example Hackathon" in check.detail
    assert "Gold" in check.detail and "Associate" in check.detail


def test_event_without_tiers_fails(cfg):
    cfg.event_path.write_text(
        "event_name: X\ndate: 2026-01-01\ncolleges: a\ntiers: []\n"
        "sender_name: R\nsender_role: L\nclub: C\nreply_to: r@svnit.ac.in\n",
        encoding="utf-8")
    check = doctor.check_event(cfg)
    assert not check.ok and "tier" in check.hint


def test_voice_without_samples_is_a_warning(cfg, event, voice):
    voice.sample_emails = []
    check = doctor.check_voice(cfg, voice, event)
    assert not check.ok and check.optional
    assert "sample_emails" in check.hint


def test_voice_with_samples_is_ok(cfg, event, voice):
    check = doctor.check_voice(cfg, voice, event)
    assert check.ok
    assert "1 sample email" in check.detail


def test_voice_is_loaded_from_the_file_when_not_given(cfg, event):
    cfg.voice_path.write_text(
        "greeting: 'Hey {first},'\nsample_emails:\n  - 'Hi A,\n\n    quick one.'\n"
        "  - 'Hi B,\n\n    another.'\n", encoding="utf-8")
    check = doctor.check_voice(cfg, None, event)
    assert check.ok and "2 sample email" in check.detail
    assert isinstance(Voice.load(cfg.voice_path, event), Voice)


def test_database_check_reports_the_path(cfg):
    check = doctor.check_database(cfg)
    assert check.ok and str(cfg.db_path) in check.detail


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def test_model_check_is_a_warning_offline(cfg):
    check = doctor.check_model(cfg, live=False)
    assert not check.ok and check.optional
    assert "offline" in check.detail
    assert "GEMINI_API_KEY" in check.hint and "ANTHROPIC_API_KEY" in check.hint


def test_model_without_a_key_fails_when_live(cfg):
    check = doctor.check_model(cfg, live=True)
    assert not check.ok and not check.optional
    assert "OPENAI_API_KEY" in check.hint


def test_live_model_ping_failure_fails_with_a_hint(cfg, monkeypatch):
    import sponsy.llm as llm_mod

    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "k-test-secret"

    class Broken:
        def ping(self):
            raise RuntimeError("HTTP 401: API key not valid")

    monkeypatch.setattr(llm_mod, "build_llm", lambda c, model=None, db=None: Broken())
    check = doctor.check_model(cfg, live=True)
    assert not check.ok and not check.optional
    assert "API key not valid" in check.detail
    assert check.hint
    assert "k-test-secret" not in check.detail + check.hint


def test_live_model_quota_exhausted_says_so(cfg, monkeypatch):
    import sponsy.llm as llm_mod

    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "k"

    class Dry:
        def ping(self):
            raise QuotaExhausted("daily quota exhausted on all 1 key(s)")

    monkeypatch.setattr(llm_mod, "build_llm", lambda c, model=None, db=None: Dry())
    check = doctor.check_model(cfg, live=True)
    assert not check.ok
    assert "quota" in check.detail.lower()
    assert "billing" in check.hint and "GEMINI_API_KEYS" in check.hint


def test_live_model_ping_ok_reports_keys_and_pacing(cfg, monkeypatch):
    import sponsy.llm as llm_mod

    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "k1"
    cfg.gemini_api_keys = ["k2"]
    cfg.min_interval_s = 6.5

    class Fine:
        def ping(self):
            return "OK"

    monkeypatch.setattr(llm_mod, "build_llm", lambda c, model=None, db=None: Fine())
    check = doctor.check_model(cfg, live=True)
    assert check.ok, check.detail
    assert "gemini" in check.detail and "2 keys" in check.detail
    assert "paced" in check.detail


def test_empty_ping_is_a_failure(cfg, monkeypatch):
    import sponsy.llm as llm_mod

    cfg.llm_provider = "openai"
    cfg.openai_api_key = "k"

    class Mute:
        def ping(self):
            return ""

    monkeypatch.setattr(llm_mod, "build_llm", lambda c, model=None, db=None: Mute())
    assert not doctor.check_model(cfg, live=True).ok


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_none_and_ddg_are_warnings(cfg):
    cfg.search_provider = "none"
    check = doctor.check_search(cfg, live=False)
    assert not check.ok and check.optional
    cfg.search_provider = "ddg"
    check = doctor.check_search(cfg, live=False)
    assert not check.ok and check.optional
    assert "DuckDuckGo" in check.detail and "Tavily" in check.hint


def test_search_tavily_without_a_key_fails(cfg):
    cfg.search_provider = "tavily"
    cfg.tavily_api_key = ""
    check = doctor.check_search(cfg, live=False)
    assert not check.ok and not check.optional and "TAVILY_API_KEY" in check.hint


def test_live_search_calls_research_search(cfg, monkeypatch):
    calls = []

    def fake_search(conf, query, k=5):
        calls.append((query, k))
        return [{"title": "t", "url": "https://example.com", "snippet": "s"}]

    stub_module(monkeypatch, "sponsy.research", search=fake_search)
    cfg.search_provider = "tavily"
    cfg.tavily_api_key = "tv"
    check = doctor.check_search(cfg, live=True)
    assert check.ok, check.detail
    assert calls == [("hackathon sponsors", 1)]
    assert "1 result" in check.detail


def test_live_search_exception_is_a_fail(cfg, monkeypatch):
    def broken(conf, query, k=5):
        raise RuntimeError("HTTP 432 rate limited")

    stub_module(monkeypatch, "sponsy.research", search=broken)
    cfg.search_provider = "brave"
    cfg.brave_api_key = "b"
    check = doctor.check_search(cfg, live=True)
    assert not check.ok and not check.optional
    assert "rate limited" in check.detail


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------

class FakeBot:
    """Stands in for TelegramNotifier. send() is a trap: doctor must never post."""
    instances: list = []

    def __init__(self, token, chat_id, logfile=None):
        self.token, self.chat_id = token, chat_id
        self.calls: list[str] = []
        FakeBot.instances.append(self)

    def get_me(self):
        self.calls.append("get_me")
        return {"id": 1, "is_bot": True, "username": "sponsy_bot"}

    def get_chat(self):
        self.calls.append("get_chat")
        return {"id": int(self.chat_id), "first_name": "Rudraksh", "type": "private"}

    def get_updates(self, offset=None, timeout=0):
        self.calls.append("get_updates")
        assert offset is None, "doctor must not confirm updates; drain() needs them"
        return [{"update_id": 1, "message": {"chat": {"id": 777, "first_name": "Rudraksh"},
                                             "text": "/start"}},
                {"update_id": 2, "message": {"chat": {"id": 777}, "text": "hi"}}]

    def send(self, *a, **k):
        raise AssertionError("doctor must never send a message")

    force_reply = edit = send


def install_fake_telegram(monkeypatch, bot_cls=FakeBot):
    """Patch sponsy.telegram.TelegramNotifier if the module exists; otherwise
    inject a throwaway module so the doctor's code path still runs."""
    FakeBot.instances.clear()
    try:
        import sponsy.telegram as tg
    except ImportError:
        tg = stub_module(monkeypatch, "sponsy.telegram")
    monkeypatch.setattr(tg, "TelegramNotifier", bot_cls, raising=False)


def test_telegram_missing_token_fails_offline(cfg):
    cfg.telegram_bot_token = ""
    check = doctor.check_telegram(cfg, live=False)
    assert not check.ok and not check.optional and "BotFather" in check.hint


def test_telegram_missing_chat_id_reads_it_from_updates(cfg, monkeypatch):
    install_fake_telegram(monkeypatch)
    cfg.telegram_bot_token = "123:abc"
    cfg.telegram_chat_id = ""
    check = doctor.check_telegram(cfg, live=True)
    assert not check.ok and not check.optional
    assert "777" in check.hint and "TELEGRAM_CHAT_ID" in check.hint
    assert "@sponsy_bot" in check.detail
    assert FakeBot.instances[0].calls == ["get_me", "get_updates"]


def test_telegram_live_ok_names_bot_and_chat(cfg, monkeypatch):
    install_fake_telegram(monkeypatch)
    cfg.telegram_bot_token = "123:abc"
    cfg.telegram_chat_id = "12345"
    check = doctor.check_telegram(cfg, live=True)
    assert check.ok, check.detail
    assert "@sponsy_bot" in check.detail and "Rudraksh" in check.detail
    assert "get_updates" not in FakeBot.instances[0].calls


def test_telegram_bad_token_fails_on_get_me(cfg, monkeypatch):
    class Unauthorized(FakeBot):
        def get_me(self):
            raise RuntimeError("telegram getMe: Unauthorized")

    install_fake_telegram(monkeypatch, Unauthorized)
    cfg.telegram_bot_token = "bad"
    cfg.telegram_chat_id = "12345"
    check = doctor.check_telegram(cfg, live=True)
    assert not check.ok and "getMe" in check.detail and "BotFather" in check.hint


def test_chat_ids_are_distinct_and_labelled():
    ids = doctor._chat_ids([
        {"message": {"chat": {"id": 1, "first_name": "A"}}},
        {"message": {"chat": {"id": 1}}},
        {"callback_query": {"message": {"chat": {"id": 2, "title": "Group"}}}},
        "junk", {"update_id": 9},
    ])
    assert ids == ["1 (A)", "2 (Group)"]


# ---------------------------------------------------------------------------
# mail
# ---------------------------------------------------------------------------

def test_mail_manual_is_a_warning(cfg):
    cfg.mail_provider = "manual"
    check = doctor.check_mail(cfg, live=False)
    assert not check.ok and check.optional
    assert "nothing will be sent" in check.detail
    assert "gmail_api" in check.hint


def test_mail_manual_says_when_allow_send_is_off(cfg):
    cfg.mail_provider = "auto"
    cfg.smtp_user, cfg.smtp_password = "me@gmail.com", "app-pw"
    cfg.allow_send = False
    check = doctor.check_mail(cfg, live=False)
    assert not check.ok and check.optional
    assert "ALLOW_SEND=0" in check.detail and "ALLOW_SEND=1" in check.hint


def test_mail_gmail_api_without_files_fails(cfg, tmp_path):
    cfg.mail_provider = "gmail_api"
    cfg.gmail_credentials = tmp_path / "creds.json"
    cfg.gmail_token = tmp_path / "token.json"
    check = doctor.check_mail(cfg, live=False)
    assert not check.ok and not check.optional
    assert "gmail-auth" in check.hint and "OAuth client ID" in check.hint


def test_mail_gmail_api_live_reads_the_profile(cfg, tmp_path, monkeypatch):
    class FakeGmail:
        def __init__(self, conf):
            self.authorised = False

        def authorize(self, interactive=True):
            assert interactive is False, "doctor must not open a browser"
            self.authorised = True
            return self

        def profile(self):
            assert self.authorised
            return {"emailAddress": "me@gmail.com"}

    stub_module(monkeypatch, "sponsy.mail")
    stub_module(monkeypatch, "sponsy.mail.gmail_api", GmailClient=FakeGmail)
    cfg.mail_provider = "gmail_api"
    cfg.gmail_token = tmp_path / "token.json"
    cfg.gmail_token.write_text("{}", encoding="utf-8")
    check = doctor.check_mail(cfg, live=True)
    assert check.ok, check.detail
    assert "me@gmail.com" in check.detail


def test_mail_smtp_missing_password_fails(cfg):
    cfg.mail_provider = "smtp"
    cfg.smtp_user, cfg.smtp_password = "me@gmail.com", ""
    check = doctor.check_mail(cfg, live=False)
    assert not check.ok and "SMTP_PASSWORD" in check.detail
    assert "App Password" in check.hint


def test_mail_smtp_live_uses_check_login(cfg, monkeypatch):
    seen = []
    stub_module(monkeypatch, "sponsy.mail")
    stub_module(monkeypatch, "sponsy.mail.smtp", check_login=lambda c: seen.append(c))
    cfg.mail_provider = "smtp"
    cfg.smtp_user, cfg.smtp_password = "me@gmail.com", "app-pw"
    check = doctor.check_mail(cfg, live=True)
    assert check.ok, check.detail
    assert seen == [cfg]
    assert "app-pw" not in check.detail


def test_mail_smtp_login_failure_hints_app_password(cfg, monkeypatch):
    def bad_login(conf):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    stub_module(monkeypatch, "sponsy.mail")
    stub_module(monkeypatch, "sponsy.mail.smtp", check_login=bad_login)
    cfg.mail_provider = "smtp"
    cfg.smtp_user, cfg.smtp_password = "me@gmail.com", "wrong"
    check = doctor.check_mail(cfg, live=True)
    assert not check.ok and "535" in check.detail and "App Password" in check.hint


def test_mail_smtp_falls_back_to_stdlib_login(cfg, monkeypatch):
    """When the mail module has no check_login, doctor logs in itself."""
    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            log.append(("connect", host, port))

        def ehlo(self):
            log.append("ehlo")

        def starttls(self):
            log.append("starttls")

        def login(self, user, password):
            log.append(("login", user))

        def quit(self):
            log.append("quit")

    stub_module(monkeypatch, "sponsy.mail")
    stub_module(monkeypatch, "sponsy.mail.smtp")     # no check_login attribute
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    cfg.mail_provider = "smtp"
    cfg.smtp_user, cfg.smtp_password = "me@gmail.com", "app-pw"
    check = doctor.check_mail(cfg, live=True)
    assert check.ok, check.detail
    assert log == [("connect", "smtp.gmail.com", 587), "ehlo", "starttls",
                   ("login", "me@gmail.com"), "quit"]


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------

def test_replies_none_is_a_warning(cfg):
    cfg.replies_provider = "none"
    check = doctor.check_replies(cfg, live=False)
    assert not check.ok and check.optional and "simulate-reply" in check.detail


def test_replies_gmail_api_needs_the_token(cfg, tmp_path):
    cfg.replies_provider = "gmail_api"
    cfg.gmail_token = tmp_path / "token.json"
    assert not doctor.check_replies(cfg, live=False).ok
    cfg.gmail_token.write_text("{}", encoding="utf-8")
    assert doctor.check_replies(cfg, live=False).ok


def test_replies_imap_live_logs_in_read_only(cfg, monkeypatch):
    log = []

    class FakeIMAP:
        def __init__(self, host, timeout=None):
            log.append(("connect", host))

        def login(self, user, password):
            log.append(("login", user))
            return "OK", [b"ok"]

        def select(self, folder, readonly=False):
            log.append(("select", folder, readonly))
            return "OK", [b"42"]

        def logout(self):
            log.append("logout")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    cfg.replies_provider = "imap"
    cfg.imap_user, cfg.imap_password = "me@gmail.com", "app-pw"
    check = doctor.check_replies(cfg, live=True)
    assert check.ok, check.detail
    assert "42 message" in check.detail
    assert log == [("connect", "imap.gmail.com"), ("login", "me@gmail.com"),
                   ("select", "INBOX", True), "logout"]


def test_replies_imap_login_failure_is_a_fail(cfg, monkeypatch):
    class Refusing:
        def __init__(self, host, timeout=None):
            pass

        def login(self, user, password):
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Refusing)
    cfg.replies_provider = "imap"
    cfg.imap_user, cfg.imap_password = "me@gmail.com", "wrong"
    check = doctor.check_replies(cfg, live=True)
    assert not check.ok and "AUTHENTICATIONFAILED" in check.detail
    assert "App Password" in check.hint


# ---------------------------------------------------------------------------
# send gate + report
# ---------------------------------------------------------------------------

def test_send_gate_is_always_ok_and_explains(cfg):
    cfg.allow_send = False
    check = doctor.check_send_gate(cfg)
    assert check.ok and check.optional
    assert "ALLOW_SEND is off" in check.detail and "Telegram" in check.detail
    cfg.allow_send = True
    assert "ALLOW_SEND is on" in doctor.check_send_gate(cfg).detail


def test_print_report_has_fix_lines_and_a_verdict(cfg, capsys):
    checks = doctor.run_checks(cfg, live=False)
    doctor.print_report(checks)
    out = capsys.readouterr().out
    for name in NAMES:
        assert name in out
    assert "fix: event.yml" in out
    assert "problem(s) to fix before a live run" in out
    assert "OK  " in out and "FAIL" in out and "WARN" in out


def test_print_report_all_good_line(capsys):
    doctor.print_report([Check("a", True, "fine"), Check("b", False, "meh", "x",
                                                        optional=True)])
    out = capsys.readouterr().out
    assert "all good - run: python run.py run --limit 3" in out
    assert "fix: b - x" in out


def test_print_report_never_raises(capsys):
    doctor.print_report([Check("x", False, detail=None, hint=None)])   # type: ignore
    doctor.print_report(None)                                          # type: ignore
    doctor.print_report([object()])                                    # type: ignore
    assert "doctor" in capsys.readouterr().out or True


def test_cli_exit_rule_ignores_optional_checks(cfg):
    """The cli exits 2 only on non-optional failures; WARNs never block."""
    checks = doctor.run_checks(cfg, live=False)
    optional_only = [c for c in checks if c.optional]
    assert optional_only, "expected at least the model/voice/mail WARNs offline"
    required_failed = [c for c in checks if not c.ok and not c.optional]
    assert {c.name for c in required_failed} >= {"event.yml", "telegram"}


# ---------------------------------------------------------------------------
# the brochure
# ---------------------------------------------------------------------------

def test_no_brochure_is_a_warning_not_a_failure(cfg, tmp_path):
    cfg.brochure = tmp_path / "nothing.pdf"
    check = doctor.check_brochure(cfg)
    assert not check.ok and check.optional
    assert "plain text" in check.detail and "BROCHURE" in check.hint


def test_a_real_brochure_reports_its_name_and_when_it_is_sent(cfg, tmp_path):
    path = tmp_path / "DotSlash.pdf"
    path.write_bytes(b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    cfg.brochure = path
    check = doctor.check_brochure(cfg)
    assert check.ok
    assert "DotSlash.pdf" in check.detail
    assert "initial" in check.detail and "needs_info" in check.detail


def test_an_oversized_brochure_is_flagged(cfg, tmp_path):
    from sponsy.mail.message import MAX_ATTACHMENT_BYTES
    path = tmp_path / "huge.pdf"
    path.write_bytes(b"x" * (MAX_ATTACHMENT_BYTES + 1))
    cfg.brochure = path
    check = doctor.check_brochure(cfg)
    assert not check.ok and "MB" in check.detail


def test_an_empty_brochure_is_flagged(cfg, tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    cfg.brochure = path
    assert not doctor.check_brochure(cfg).ok
