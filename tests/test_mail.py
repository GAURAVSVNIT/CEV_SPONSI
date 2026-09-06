"""Delivery: the right channel on approval, and what happens when a provider
says no. Offline: every provider is monkeypatched, the clipboard is stubbed."""

from __future__ import annotations

import base64
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from sponsy import mail
from sponsy.mail import gmail_api, message
from sponsy.mail.gmail_api import GmailClient, GmailNotConfigured, parse_gmail_message

TODAY = date(2026, 9, 5)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clipboard(monkeypatch):
    """Never touch the developer's real clipboard; record the attempts."""
    calls: list[str] = []

    def fake(text: str) -> bool:
        calls.append(text)
        return True

    monkeypatch.setattr(mail, "to_clipboard", fake)
    return calls


@pytest.fixture
def smtp_cfg(cfg):
    cfg.mail_provider = "smtp"
    cfg.smtp_host = "smtp.example.com"
    cfg.smtp_user = "club@example.org"
    cfg.smtp_password = "app-password"
    cfg.smtp_from = "club@example.org"
    cfg.allow_send = True
    cfg.dry_run = False
    return cfg


@pytest.fixture
def gmail_cfg(cfg, tmp_path):
    cfg.mail_provider = "gmail_api"
    cfg.gmail_sender = "rudraksh@gmail.com"
    cfg.gmail_token = tmp_path / "token.json"
    cfg.gmail_credentials = tmp_path / "creds.json"
    cfg.allow_send = True
    cfg.dry_run = False
    return cfg


class FakeGmail:
    """Stands in for GmailClient inside deliver(). Records what was sent."""
    sent: list[dict] = []
    fail: Exception | None = None

    def __init__(self, cfg):
        self.cfg = cfg

    def authorize(self, interactive=True):
        assert interactive is False, "deliver must never open a browser"
        if FakeGmail.fail is not None:
            raise FakeGmail.fail
        return self

    def send(self, msg, thread_id=None):
        FakeGmail.sent.append({"raw": msg.as_bytes(), "threadId": thread_id,
                               "message_id": msg["Message-ID"]})
        return {"message_id": msg["Message-ID"], "gmail_id": "g1",
                "thread_id": thread_id or "t-new"}


@pytest.fixture
def fake_gmail(monkeypatch):
    FakeGmail.sent = []
    FakeGmail.fail = None
    monkeypatch.setattr(mail, "GmailClient", FakeGmail)
    return FakeGmail


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def canned_gmail_message(with_text: bool = True) -> dict:
    """users.messages.get(format="full"): multipart/mixed wrapping a
    multipart/alternative, the shape Gmail produces for a reply with an
    attachment."""
    alternative_parts = [
        {"mimeType": "text/html", "body": {"data": _b64url("<p>Sounds good</p>")}},
    ]
    if with_text:
        alternative_parts.insert(0, {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type",
                         "value": 'text/plain; charset="UTF-8"'}],
            "body": {"data": _b64url("Sounds good, send the deck over.\r\n\r\n"
                                     "On Fri, Rudraksh wrote:\r\n> hi")}})
    return {
        "id": "18f3a2b1c0d9e8f7",
        "threadId": "18f3a2b1c0d9e000",
        "snippet": "Sounds good, send the deck over.",
        "internalDate": "1788566400000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Devi Sharma <devi@example.com>"},
                {"name": "Subject", "value": "Re: Example Hackathon sponsorship"},
                {"name": "Message-ID", "value": "<reply-1@mail.example.com>"},
                {"name": "In-Reply-To", "value": "<ours-1@example.org>"},
                {"name": "References", "value": "<ours-1@example.org> <ours-0@example.org>"},
                {"name": "Date", "value": "Sat, 05 Sep 2026 05:30:00 +0530"},
            ],
            "parts": [
                {"mimeType": "multipart/alternative", "parts": alternative_parts},
                {"mimeType": "application/pdf", "filename": "deck.pdf",
                 "body": {"attachmentId": "att-1", "size": 1024}},
            ],
        },
    }


class FakeService:
    """The googleapiclient resource chain, just deep enough for our calls."""

    def __init__(self, listing=None, full=None):
        self.calls: list[tuple] = []
        self.listing = listing or {"messages": []}
        self.full = full or canned_gmail_message()

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, userId):
        return self._exec({"emailAddress": "rudraksh@gmail.com"})

    def send(self, userId, body):
        self.calls.append(("send", userId, body))
        return self._exec({"id": "g-42", "threadId": body.get("threadId") or "t-42"})

    def list(self, userId, q, maxResults):
        self.calls.append(("list", userId, q, maxResults))
        return self._exec(self.listing)

    def get(self, userId, id, format):
        self.calls.append(("get", userId, id, format))
        return self._exec(dict(self.full, id=id))

    @staticmethod
    def _exec(value):
        return SimpleNamespace(execute=lambda: value)


# ---------------------------------------------------------------------------
# channel selection
# ---------------------------------------------------------------------------

def test_smtp_is_off_without_a_password(cfg):
    cfg.mail_provider = "auto"
    cfg.allow_send = True
    cfg.smtp_host = "smtp.example.com"
    cfg.smtp_user = "club@example.org"
    cfg.smtp_password = ""
    assert not cfg.smtp_enabled, "announcing SMTP with no password loses emails"
    assert cfg.resolved_mail_provider() == "manual"


def test_approved_email_goes_out_over_smtp(smtp_cfg, event, voice, prospect,
                                           monkeypatch):
    sent = {}

    def fake_send(cfg_, msg):
        sent["to"] = msg["To"]
        sent["id"] = msg["Message-ID"]
        return msg["Message-ID"]

    monkeypatch.setattr(mail, "send_smtp", fake_send)
    out = mail.deliver(smtp_cfg, event, voice, prospect, "Sponsoring Example Hackathon",
                       "Hi Devi,\n\nBody.", "initial", today=TODAY)
    assert out["channel"] == "smtp"
    assert "devi@example.com" in sent["to"]
    assert sent["id"] == out["message_id"], "the Message-ID must be the one we store"
    assert "error" not in out
    assert out["thread_id"] is None


def test_a_message_id_is_set_so_replies_can_be_threaded(smtp_cfg, event, voice,
                                                        prospect, monkeypatch):
    monkeypatch.setattr(mail, "send_smtp", lambda c, m: m["Message-ID"])
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["message_id"].startswith("<") and out["message_id"].endswith(">")
    assert out["message_id"].endswith("@example.org>"), "domain of the sender"


def test_smtp_failure_falls_back_instead_of_losing_the_email(smtp_cfg, event, voice,
                                                            prospect, monkeypatch,
                                                            clipboard):
    """You already approved it. A dead mail server must not eat it."""
    def boom(cfg_, msg):
        raise OSError("connection refused")

    monkeypatch.setattr(mail, "send_smtp", boom)
    out = mail.deliver(smtp_cfg, event, voice, prospect, "Subject here",
                       "Hi Devi,\n\nBody.", "initial", today=TODAY)
    assert out["channel"] == "manual"
    assert out["error"].startswith("OSError: connection refused")
    assert out["mailto"].startswith("mailto:devi@example.com")
    assert Path(out["eml"]).exists(), "the .eml must still be on disk"
    assert out["copied"] is True and clipboard, "clipboard gets the text as a fallback"
    assert "Subject: Subject here" in clipboard[0]


def test_eml_is_on_disk_before_any_provider_runs(smtp_cfg, event, voice, prospect,
                                                 monkeypatch):
    seen = {}

    def check_then_fail(cfg_, msg):
        seen["files"] = list(Path(cfg_.outbox).glob("P01-initial-*.eml"))
        raise RuntimeError("provider down")

    monkeypatch.setattr(mail, "send_smtp", check_then_fail)
    mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial", today=TODAY)
    assert seen["files"], "write_eml must run before the provider is tried"


def test_dry_run_never_touches_a_provider(smtp_cfg, event, voice, prospect,
                                          monkeypatch, clipboard):
    def boom(*args, **kwargs):
        raise AssertionError("dry run must not send")

    monkeypatch.setattr(mail, "send_smtp", boom)
    monkeypatch.setattr(mail, "GmailClient", boom)
    smtp_cfg.dry_run = True
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual"
    assert "error" not in out
    assert Path(out["eml"]).exists()
    assert not clipboard, "a dry run leaves the clipboard alone too"


def test_allow_send_false_forces_manual(smtp_cfg, event, voice, prospect, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ALLOW_SEND=0 must not send")

    monkeypatch.setattr(mail, "send_smtp", boom)
    monkeypatch.setattr(mail, "GmailClient", boom)
    smtp_cfg.allow_send = False
    assert smtp_cfg.resolved_mail_provider() == "manual"
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual"
    assert "error" not in out
    assert out["copied"] is True


# ---------------------------------------------------------------------------
# gmail_api channel through deliver()
# ---------------------------------------------------------------------------

def test_gmail_api_channel_sends_in_the_prospects_thread(gmail_cfg, event, voice,
                                                         prospect, fake_gmail):
    out = mail.deliver(gmail_cfg, event, voice, prospect, "Re: sponsorship",
                       "Quick one.", "nudge", today=TODAY,
                       in_reply_to="<ours-1@example.org>", thread_id="t-123")
    assert out["channel"] == "gmail_api"
    assert out["thread_id"] == "t-123"
    assert out["gmail_id"] == "g1"
    assert "error" not in out
    assert len(fake_gmail.sent) == 1
    sent = fake_gmail.sent[0]
    assert sent["threadId"] == "t-123"
    assert sent["message_id"] == out["message_id"]
    assert b"Subject: Re: sponsorship" in sent["raw"]
    assert b"In-Reply-To: <ours-1@example.org>" in sent["raw"]
    assert b"From: Rudraksh <rudraksh@gmail.com>" in sent["raw"]


def test_gmail_api_first_email_gets_a_new_thread_id(gmail_cfg, event, voice, prospect,
                                                    fake_gmail):
    out = mail.deliver(gmail_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "gmail_api"
    assert out["thread_id"] == "t-new", "stored so follow-ups can thread"


def test_gmail_api_failure_falls_back_to_manual(gmail_cfg, event, voice, prospect,
                                                fake_gmail, clipboard):
    fake_gmail.fail = GmailNotConfigured("no token; run python run.py gmail-auth")
    out = mail.deliver(gmail_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual"
    assert out["error"].startswith("GmailNotConfigured: no token")
    assert out["thread_id"] is None
    assert Path(out["eml"]).exists()
    assert clipboard and out["copied"] is True


# ---------------------------------------------------------------------------
# the message
# ---------------------------------------------------------------------------

def test_reply_threading_headers_are_set(smtp_cfg, event, voice, prospect):
    msg = message.build_message(smtp_cfg, event, voice, prospect, "Re: hello", "body",
                                in_reply_to="<their-message@example.com>")
    assert msg["In-Reply-To"] == "<their-message@example.com>"
    assert msg["References"] == "<their-message@example.com>"
    first = message.build_message(smtp_cfg, event, voice, prospect, "hello", "body")
    assert first["In-Reply-To"] is None and first["References"] is None


def test_from_uses_the_voice_sender_name(cfg, event, voice, prospect):
    voice.sender_name = "Rudy Patel"
    msg = message.build_message(cfg, event, voice, prospect, "s", "b")
    assert msg["From"] == "Rudy Patel <club@example.org>", \
        "voice name, and event.reply_to when no provider account is set"
    assert msg["To"] == "Devi Sharma <devi@example.com>"
    assert msg["Reply-To"] == "club@example.org"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip().startswith("b")
    assert msg["Date"]


def test_gmail_sender_is_the_from_address_on_the_api_channel(gmail_cfg, event, voice,
                                                             prospect):
    msg = message.build_message(gmail_cfg, event, voice, prospect, "s", "b")
    assert msg["From"] == "Rudraksh <rudraksh@gmail.com>"
    assert msg["Reply-To"] == "club@example.org"
    assert msg["Message-ID"].endswith("@gmail.com>")


def test_a_comma_in_the_contact_name_is_quoted(cfg, event, voice):
    target = SimpleNamespace(id="P02", email="d@example.com", contact_name="Sharma, Devi")
    msg = message.build_message(cfg, event, voice, target, "s", "b")
    assert msg["To"] == '"Sharma, Devi" <d@example.com>'
    assert len(msg["To"].addresses) == 1, "an unquoted comma would make two recipients"


def test_mailto_url_encodes_subject_and_body(prospect):
    url = message.mailto_url(prospect, "Quick one", "Hi Devi,\n\nBody & more")
    assert url.startswith("mailto:devi@example.com?")
    assert "subject=Quick%20one" in url
    assert "body=Hi%20Devi%2C%0A%0ABody%20%26%20more" in url


def test_write_eml_produces_a_safe_filename(cfg, event, voice):
    target = SimpleNamespace(id="P/01 odd:id", email="d@example.com", contact_name="")
    msg = message.build_message(cfg, event, voice, target, "s", "b")
    path = message.write_eml(cfg, msg, target, "initial", TODAY)
    assert path.name == "P_01_odd_id-initial-2026-09-05.eml"
    assert path.parent == Path(cfg.outbox)
    assert b"Subject: s" in path.read_bytes()
    again = message.write_eml(cfg, msg, target, "initial", TODAY)
    assert again.name == "P_01_odd_id-initial-2026-09-05-2.eml", \
        "a second send the same day must not overwrite the first record"


# ---------------------------------------------------------------------------
# test_send
# ---------------------------------------------------------------------------

def test_test_send_addresses_the_reply_to_and_stays_manual_offline(cfg, event, voice):
    out = mail.test_send(cfg, event, voice)
    assert out["to"] == event.reply_to == "club@example.org"
    assert out["channel"] == "manual"
    assert "error" not in out
    eml = Path(out["eml"])
    assert eml.exists() and eml.name.startswith("TEST-initial-")
    raw = eml.read_bytes()
    assert b"To: Rudraksh <club@example.org>" in raw
    assert b"Subject: sponsy test email" in raw
    # It goes through deliver() and the real template, so what lands in your
    # inbox is shaped exactly like what a sponsor would get.
    assert b"This is a test of the" in raw
    assert b"text/html" in raw and event.event_name.encode() in raw


def test_test_send_reports_a_missing_address(cfg, event, voice):
    event.reply_to = ""
    cfg.gmail_sender = ""
    cfg.smtp_user = ""
    out = mail.test_send(cfg, event, voice)
    assert out["to"] == ""
    assert "no address" in out["error"]


# ---------------------------------------------------------------------------
# GmailClient: parsing and the API calls, with a fake service
# ---------------------------------------------------------------------------

def test_parse_gmail_message_walks_nested_parts():
    raw = parse_gmail_message(canned_gmail_message())
    assert raw.uid == "18f3a2b1c0d9e8f7"
    assert raw.thread_id == "18f3a2b1c0d9e000"
    assert raw.message_id == "<reply-1@mail.example.com>"
    assert raw.from_addr == "devi@example.com"
    assert raw.subject == "Re: Example Hackathon sponsorship"
    assert raw.refs == ["<ours-1@example.org>", "<ours-0@example.org>"]
    assert raw.body.startswith("Sounds good, send the deck over.\n\nOn Fri")
    assert "\r" not in raw.body
    assert raw.received_at.startswith("2026-09-05T00:00:00")


def test_parse_gmail_message_falls_back_to_the_snippet():
    raw = parse_gmail_message(canned_gmail_message(with_text=False))
    assert raw.body == "Sounds good, send the deck over."
    assert raw.refs == ["<ours-1@example.org>", "<ours-0@example.org>"]


def test_parse_gmail_message_survives_an_empty_payload():
    raw = parse_gmail_message({"id": "x", "threadId": None, "snippet": ""})
    assert raw.uid == "x" and raw.body == "" and raw.refs == []
    assert raw.from_addr == "" and raw.received_at == ""


def test_authorize_fails_fast_without_token_or_credentials(gmail_cfg, monkeypatch):
    """The check must run before any google import: a cron box or a laptop
    without the packages gets the real reason, not a pip error."""
    monkeypatch.setitem(sys.modules, "google", None)          # import -> ImportError
    client = GmailClient(gmail_cfg)
    assert not client.is_authorized
    with pytest.raises(GmailNotConfigured) as info:
        client.authorize(interactive=False)
    text = str(info.value)
    assert "gmail-auth" in text and str(gmail_cfg.gmail_credentials) in text
    assert "pip install" not in text
    with pytest.raises(GmailNotConfigured):
        client.authorize(interactive=True)      # no client secrets file either


def test_send_encodes_the_message_as_base64url_with_the_thread(gmail_cfg, event, voice,
                                                               prospect):
    client = GmailClient(gmail_cfg)
    client.creds = object()
    client._service = service = FakeService()
    msg = message.build_message(gmail_cfg, event, voice, prospect, "Re: hi", "body",
                                in_reply_to="<ours-1@example.org>")
    out = client.send(msg, thread_id="t-9")
    kind, user, body = service.calls[0]
    assert (kind, user) == ("send", "me")
    assert body["threadId"] == "t-9"
    decoded = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
    assert b"Subject: Re: hi" in decoded and b"\r\n" in decoded
    assert out == {"message_id": msg["Message-ID"], "gmail_id": "g-42",
                   "thread_id": "t-9"}
    client.send(msg)
    assert "threadId" not in service.calls[1][2], "no thread for a first email"


def test_list_replies_queries_the_inbox_since_a_date(gmail_cfg):
    client = GmailClient(gmail_cfg)
    client.creds = object()
    client._service = service = FakeService(
        listing={"messages": [{"id": "new"}, {"id": "old"}]})
    out = client.list_replies(date(2026, 8, 6), limit=25)
    assert service.calls[0] == ("list", "me", "in:inbox after:2026/08/06 -from:me", 25)
    assert [c[2] for c in service.calls[1:]] == ["new", "old"]
    assert service.calls[1][3] == "full"
    assert [r.uid for r in out] == ["old", "new"], "oldest first"
    assert out[0].from_addr == "devi@example.com"
    assert client.profile()["emailAddress"] == "rudraksh@gmail.com"


def test_raw_mail_fields(gmail_cfg):
    raw = gmail_api.RawMail(uid="1", message_id="<m>", from_addr="a@b.c",
                            subject="s", body="b", refs=["<x>"])
    assert raw.thread_id is None and raw.received_at == ""
    assert mail.RawMail is gmail_api.RawMail


# ---------------------------------------------------------------------------
# the brochure
# ---------------------------------------------------------------------------

MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
               b"trailer<</Root 1 0 R>>\n%%EOF\n")


@pytest.fixture
def brochure(cfg, tmp_path):
    path = tmp_path / "DotSlash-10-brochure.pdf"
    path.write_bytes(MINIMAL_PDF)
    cfg.brochure = path
    return path


def _parts(msg):
    return [(p.get_content_type(), p.get_filename()) for p in msg.walk()]


def test_the_first_email_carries_the_brochure(cfg, event, voice, prospect,
                                              brochure):
    msg = message.build_message(cfg, event, voice, prospect, "s", "body",
                                attachment=message.attachment_for(cfg, "initial"))
    assert msg.is_multipart()
    assert ("application/pdf", brochure.name) in _parts(msg)
    assert MINIMAL_PDF in bytes(msg) or brochure.name in str(msg)


def test_an_answer_to_a_question_carries_it_too(cfg, brochure):
    assert message.attachment_for(cfg, "needs_info") == brochure


@pytest.mark.parametrize("kind", ["nudge", "new_angle", "last_touch"])
def test_the_follow_ups_do_not_re_send_it(cfg, brochure, kind):
    """The same PDF three times is how a thread starts looking like spam."""
    assert message.attachment_for(cfg, kind) is None


def test_no_brochure_means_no_attachment(cfg, event, voice, prospect):
    """The message is still multipart -- text plus HTML -- just without a file."""
    assert message.attachment_for(cfg, "initial") is None
    msg = message.build_message(cfg, event, voice, prospect, "s", "body")
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == []


def test_a_missing_file_is_not_an_error(cfg, tmp_path):
    cfg.brochure = tmp_path / "gone.pdf"
    assert cfg.brochure_file is None
    assert message.attachment_for(cfg, "initial") is None


@pytest.mark.parametrize("make,fragment", [
    (lambda p: p.write_bytes(b""), "empty"),
    (lambda p: p.write_bytes(b"x" * (message.MAX_ATTACHMENT_BYTES + 1)), "MB"),
])
def test_an_unusable_brochure_is_reported_not_attached(cfg, tmp_path, make,
                                                       fragment):
    path = tmp_path / "bad.pdf"
    make(path)
    assert fragment in message.attachment_problem(path)


def test_a_good_brochure_has_no_problem(brochure):
    assert message.attachment_problem(brochure) == ""
    assert message.attachment_problem(None) == ""


def test_deliver_attaches_it_and_says_so(smtp_cfg, event, voice, prospect,
                                         brochure, monkeypatch):
    sent = {}
    monkeypatch.setattr(mail, "send_smtp",
                        lambda cfg_, msg: sent.update(msg=msg) or msg["Message-ID"])
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "smtp"
    assert out["attached"] == brochure.name
    assert "attach_error" not in out
    assert ("application/pdf", brochure.name) in _parts(sent["msg"])


def test_the_eml_on_disk_contains_the_attachment(smtp_cfg, event, voice, prospect,
                                                 brochure, monkeypatch):
    """The .eml is the copy that survives a provider failure, so it is the one
    that has to be complete."""
    monkeypatch.setattr(mail, "send_smtp", lambda cfg_, msg: (_ for _ in ()).throw(
        OSError("connection refused")))
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual" and out["attached"] == brochure.name
    raw = Path(out["eml"]).read_bytes()
    assert brochure.name.encode() in raw
    assert b"application/pdf" in raw


def test_a_broken_brochure_never_stops_the_email(smtp_cfg, event, voice, prospect,
                                                 tmp_path, monkeypatch):
    smtp_cfg.brochure = tmp_path / "huge.pdf"
    smtp_cfg.brochure.write_bytes(b"x" * (message.MAX_ATTACHMENT_BYTES + 1))
    monkeypatch.setattr(mail, "send_smtp", lambda cfg_, msg: msg["Message-ID"])
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "smtp", "the pitch matters more than the PDF"
    assert out["attached"] is None
    assert "MB" in out["attach_error"]


def test_a_follow_up_delivers_without_it(smtp_cfg, event, voice, prospect,
                                         brochure, monkeypatch):
    monkeypatch.setattr(mail, "send_smtp", lambda cfg_, msg: msg["Message-ID"])
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "nudge",
                       today=TODAY)
    assert out["attached"] is None and out["channel"] == "smtp"


def test_gmail_sends_the_attachment_too(gmail_cfg, event, voice, prospect,
                                        brochure, fake_gmail):
    out = mail.deliver(gmail_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "gmail_api" and out["attached"] == brochure.name
    assert brochure.name.encode() in fake_gmail.sent[0]["raw"]


def test_the_filename_the_recipient_sees_is_yours(cfg, event, voice, prospect,
                                                  tmp_path):
    path = tmp_path / "DotSlash 10.0 Sponsorship.pdf"
    path.write_bytes(MINIMAL_PDF)
    cfg.brochure = path
    msg = message.build_message(cfg, event, voice, prospect, "s", "b",
                                attachment=path)
    assert ("application/pdf", "DotSlash 10.0 Sponsorship.pdf") in _parts(msg)


def test_a_non_pdf_still_attaches(cfg, event, voice, prospect, tmp_path):
    path = tmp_path / "brief.docx"
    path.write_bytes(b"PK\x03\x04 not really a docx")
    cfg.brochure = path
    msg = message.build_message(cfg, event, voice, prospect, "s", "b",
                                attachment=path)
    assert any(name == "brief.docx" for _ctype, name in _parts(msg))


# ---------------------------------------------------------------------------
# SEND_ONLY_TO: the test-phase safety valve
# ---------------------------------------------------------------------------

def test_with_no_allowlist_everyone_is_reachable(cfg):
    assert cfg.send_only_to == []
    assert cfg.may_send_to("anyone@example.com")


def test_the_allowlist_is_case_and_space_insensitive(cfg):
    cfg.send_only_to = ["me@gmail.com"]
    assert cfg.may_send_to("  ME@Gmail.com ")
    assert not cfg.may_send_to("devi@example.com")


def test_an_address_on_the_list_is_delivered(smtp_cfg, event, voice, prospect,
                                             monkeypatch):
    smtp_cfg.send_only_to = [prospect.email]
    monkeypatch.setattr(mail, "send_smtp", lambda cfg_, msg: msg["Message-ID"])
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "smtp" and "blocked" not in out


def test_everyone_else_is_held_and_nothing_is_sent(smtp_cfg, event, voice, prospect,
                                                   monkeypatch):
    """The dangerous moment is the one where mail works for the first time and
    a real prospect's card is still one tap away."""
    smtp_cfg.send_only_to = ["me@gmail.com"]
    monkeypatch.setattr(mail, "send_smtp", lambda cfg_, msg: (_ for _ in ()).throw(
        AssertionError("must not reach the provider")))
    out = mail.deliver(smtp_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual"
    assert prospect.email in out["blocked"] and "SEND_ONLY_TO" in out["blocked"]
    assert "error" not in out, "held is not the same as failed"
    assert Path(out["eml"]).exists(), "the approved email is still on disk"


def test_gmail_is_held_by_the_allowlist_too(gmail_cfg, event, voice, prospect,
                                            fake_gmail):
    gmail_cfg.send_only_to = ["me@gmail.com"]
    out = mail.deliver(gmail_cfg, event, voice, prospect, "s", "b", "initial",
                       today=TODAY)
    assert out["channel"] == "manual" and out.get("blocked")
    assert fake_gmail.sent == []


def test_manual_delivery_ignores_the_allowlist(cfg, event, voice, prospect):
    """Nothing leaves the machine in manual mode anyway."""
    cfg.mail_provider = "manual"
    cfg.dry_run = False
    cfg.send_only_to = ["me@gmail.com"]
    out = mail.deliver(cfg, event, voice, prospect, "s", "b", "initial", today=TODAY)
    assert out["channel"] == "manual" and "blocked" not in out


def test_test_send_can_be_pointed_at_your_own_address(cfg, event, voice):
    out = mail.test_send(cfg, event, voice, to="me@gmail.com")
    assert out["to"] == "me@gmail.com"


def test_test_send_defaults_to_the_event_reply_to(cfg, event, voice):
    assert mail.test_send(cfg, event, voice)["to"] == event.reply_to


# ---------------------------------------------------------------------------
# logos: embedded, never linked
# ---------------------------------------------------------------------------

def _png(path, colour=b"\x00"):
    """A 1x1 PNG, enough for the MIME plumbing."""
    import base64
    path.write_bytes(base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
        b"IQAAAABJRU5ErkJggg=="))
    return path


@pytest.fixture
def logos(event, tmp_path):
    folder = tmp_path / "logos"
    folder.mkdir()
    for name in ("svnit.png", "acm.png", "dotslash.png"):
        _png(folder / name)
    event.logo_dir = folder
    event.organisers = [{"name": "SVNIT Surat", "logo": "svnit.png"},
                        {"name": "ACM NIT Surat", "logo": "acm.png"},
                        {"name": "No Logo Org"}]
    event.brand_logo = "dotslash.png"
    return folder


def test_logos_travel_inside_the_message(cfg, event, voice, prospect, logos):
    """A logo fetched from a URL is blocked by default in Gmail and renders as
    a broken box, so they are embedded as related parts instead."""
    msg = message.build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nBody.",
                                kind="initial")
    cids = [p.get("Content-ID") for p in msg.walk() if p.get("Content-ID")]
    assert sorted(cids) == ["<logo-acm>", "<logo-dotslash>", "<logo-svnit>"]
    assert "multipart/related" in [p.get_content_type() for p in msg.walk()]


def test_the_html_points_at_the_embedded_copies(event, voice, logos):
    from sponsy.mail.render import render_html
    html = render_html(event, voice, "Hi Devi,\n\nBody.")
    assert 'src="cid:logo-svnit"' in html
    assert 'src="cid:logo-dotslash"' in html
    assert "http://" not in html and "https://" not in html.replace(
        event.website, "")


def test_an_organiser_without_a_logo_still_gets_its_name(event, voice, logos):
    from sponsy.mail.render import render_html
    assert "No Logo Org" in render_html(event, voice, "Hi Devi,\n\nBody.")


def test_a_missing_logo_file_is_skipped_not_fatal(cfg, event, voice, prospect,
                                                  logos):
    (logos / "acm.png").unlink()
    msg = message.build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nB.",
                                kind="initial")
    cids = [p.get("Content-ID") for p in msg.walk() if p.get("Content-ID")]
    assert "<logo-acm>" not in cids and "<logo-svnit>" in cids


def test_a_follow_up_carries_only_the_wordmark(cfg, event, voice, prospect, logos):
    """The nudge does not repeat the organiser row, so it does not need those
    files riding along either."""
    msg = message.build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nB.",
                                kind="nudge")
    cids = [p.get("Content-ID") for p in msg.walk() if p.get("Content-ID")]
    assert cids == ["<logo-dotslash>"]


def test_no_logo_dir_is_not_an_error(cfg, event, voice, prospect):
    event.logo_dir = None
    msg = message.build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nB.",
                                kind="initial")
    assert [p.get("Content-ID") for p in msg.walk() if p.get("Content-ID")] == []


def test_the_sponsor_strip_is_embedded_with_the_names_as_alt_text(event, voice,
                                                                  logos):
    """One image of the whole row, like the 9.0 email. A client with images
    off still reads the names."""
    from sponsy.mail.render import render_html
    _png(logos / "sponsors.png")
    event.past_sponsors_logo = "sponsors.png"
    event.past_sponsors = ["GitHub", "Twilio"]
    html = render_html(event, voice, "Hi Devi,\n\nBody.")
    assert 'src="cid:logo-sponsors"' in html
    assert 'alt="GitHub, Twilio"' in html


def test_without_a_strip_the_names_are_printed(event, voice, logos):
    from sponsy.mail.render import render_html
    event.past_sponsors_logo = ""
    event.past_sponsors = ["GitHub", "Twilio"]
    html = render_html(event, voice, "Hi Devi,\n\nBody.")
    assert "cid:logo-sponsors" not in html and "GitHub" in html


def test_lets_partner_is_a_mailto_button(event, voice):
    from sponsy.mail.render import partner_mailto, render_html
    html = render_html(event, voice, "Hi Devi,\n\nBody.", company="Postman")
    link = partner_mailto(event, "Postman")
    assert link.startswith("mailto:" + event.reply_to)
    assert "Sponsoring%20Example%20Hackathon%20-%20Postman" in link
    assert 'href="{}"'.format(link.replace("&", "&amp;")) in html
    # bulletproof button: colour on the cell, padding on the anchor
    assert 'bgcolor="#6c47c7"' in html
    assert "padding:13px 30px" in html


def test_the_button_degrades_to_text_without_a_reply_address(event, voice):
    from sponsy.mail.render import partner_mailto, render_html
    event.reply_to = ""
    assert partner_mailto(event) == ""
    assert "LET&#39;S PARTNER" in render_html(event, voice, "Hi Devi,\n\nBody.")


def test_the_plain_text_says_where_to_write(event, voice):
    from sponsy.mail.render import render_text
    text = render_text(event, voice, "Hi Devi,\n\nBody.")
    assert "reply to this email or write to " + event.reply_to in text


# ---------------------------------------------------------------------------
# SMTP: a few megabytes is a slow upload, not a broken connection
# ---------------------------------------------------------------------------

class FakeSMTP:
    """Records the timeout it was built with, and can fail on cue."""
    made: list = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.made.append({"host": host, "port": port, "timeout": timeout})
        self.sent = []

    def ehlo(self): pass
    def starttls(self): pass
    def login(self, user, password): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def send_message(self, msg):
        FakeSMTP.made[-1]["sent"] = True


@pytest.fixture
def fake_smtp(monkeypatch):
    import smtplib
    FakeSMTP.made = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_the_upload_gets_a_long_timeout(smtp_cfg, event, voice, prospect, fake_smtp):
    """A brochure plus embedded logos is megabytes; base64 adds a third. Thirty
    seconds is a handshake timeout, not an upload one."""
    from sponsy.mail.smtp import UPLOAD_TIMEOUT, send_smtp
    msg = message.build_message(smtp_cfg, event, voice, prospect, "s", "b")
    send_smtp(smtp_cfg, msg)
    assert fake_smtp.made[0]["timeout"] == UPLOAD_TIMEOUT >= 120


def test_a_dropped_connection_is_retried_once(smtp_cfg, event, voice, prospect,
                                              monkeypatch):
    import smtplib
    calls = {"n": 0}

    class Flaky(FakeSMTP):
        def send_message(self, msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise smtplib.SMTPServerDisconnected("Server not connected")

    FakeSMTP.made = []
    monkeypatch.setattr(smtplib, "SMTP", Flaky)
    from sponsy.mail.smtp import send_smtp
    msg = message.build_message(smtp_cfg, event, voice, prospect, "s", "b")
    assert send_smtp(smtp_cfg, msg) == msg["Message-ID"]
    assert calls["n"] == 2, "the approval is already recorded; try once more"


def test_two_failures_still_reach_the_caller(smtp_cfg, event, voice, prospect,
                                             monkeypatch):
    """deliver() turns this into a manual fallback with the reason on the card,
    so it must not be swallowed here."""
    import smtplib

    class Dead(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPServerDisconnected("Server not connected")

    FakeSMTP.made = []
    monkeypatch.setattr(smtplib, "SMTP", Dead)
    from sponsy.mail.smtp import send_smtp
    msg = message.build_message(smtp_cfg, event, voice, prospect, "s", "b")
    with pytest.raises(smtplib.SMTPServerDisconnected):
        send_smtp(smtp_cfg, msg)
