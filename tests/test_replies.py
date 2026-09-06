"""Replies: the rules, the status transitions, and both transports. Offline:
the Gmail client is a fake, IMAP is a fake connection, the model is FakeLLM."""

from __future__ import annotations

import imaplib
import json
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage

import pytest

from sponsy import replies, store
from sponsy.llm import FakeLLM, LLMResponse
from sponsy.models import Draft

try:
    from .conftest import TODAY
except ImportError:                      # tests/ without an __init__.py
    from conftest import TODAY


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sent_prospect(db):
    """P01 after its first email went out: one touch, a nudge due in 3 days."""
    db.execute("UPDATE prospects SET status='sent', touches=1, first_touch=?, "
               "last_touch=?, next_touch=? WHERE id='P01'",
               (TODAY.isoformat(), TODAY.isoformat(),
                (TODAY + timedelta(days=3)).isoformat()))
    return store.get(db, "P01")


def _gmail_names(monkeypatch):
    """(RawMail, GmailNotConfigured) from the real sponsy.mail.gmail_api when
    it imports; otherwise from a throwaway stub injected into sys.modules for
    this test only, so this file does not wait on the mail package."""
    try:
        from sponsy.mail.gmail_api import GmailNotConfigured, RawMail
        return RawMail, GmailNotConfigured
    except ImportError:
        pass

    @dataclass
    class RawMail:
        uid: str
        message_id: str
        from_addr: str
        subject: str
        body: str
        refs: list = field(default_factory=list)
        thread_id: str | None = None
        received_at: str | None = None

    class GmailNotConfigured(RuntimeError):
        pass

    class GmailClient:
        def __init__(self, cfg):
            raise GmailNotConfigured("stub client: no token")

    stub = types.ModuleType("sponsy.mail.gmail_api")
    stub.RawMail, stub.GmailNotConfigured, stub.GmailClient = (
        RawMail, GmailNotConfigured, GmailClient)
    if "sponsy.mail" not in sys.modules:
        pkg = types.ModuleType("sponsy.mail")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "sponsy.mail", pkg)
    monkeypatch.setitem(sys.modules, "sponsy.mail.gmail_api", stub)
    return RawMail, GmailNotConfigured


class FakeGmail:
    def __init__(self, mails):
        self.mails = mails
        self.authorized = None
        self.asked = None

    def authorize(self, interactive=True):
        self.authorized = interactive
        return self

    def list_replies(self, since, limit=40):
        self.asked = (since, limit)
        return list(self.mails)[:limit]


def _rfc822(from_addr, subject, body, message_id, in_reply_to=None,
            references=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Devi Sharma <{}>".format(from_addr)
    msg["To"] = "club@example.org"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Fri, 04 Sep 2026 10:15:00 +0530"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """imaplib.IMAP4_SSL stand-in: one mailbox with the messages given."""
    instances: list = []

    def __init__(self, host):
        self.host = host
        self.ops: list = []
        self.messages: dict[int, bytes] = dict(FakeIMAP.mailbox)
        FakeIMAP.instances.append(self)

    mailbox: dict[int, bytes] = {}

    def login(self, user, password):
        self.ops.append(("login", user))

    def select(self, folder, readonly=False):
        self.ops.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.ops.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            raw = self.messages[uid]
            return "OK", [("{} (RFC822 {{{}}}".format(uid, len(raw)).encode(), raw),
                          b")"]
        raise AssertionError("unexpected IMAP command " + command)

    def logout(self):
        self.ops.append(("logout",))


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def test_declined_cancels_the_ladder_permanently(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: Example Hackathon",
        "Thanks for reaching out, but we will pass this year. No budget left.",
        message_id="<r1>", today=TODAY)
    assert event.label == "declined"
    after = store.get(db, "P01")
    assert after.status == "declined"
    assert after.next_touch is None
    assert store.is_blocked(db, "devi@example.com")
    assert store.due_today(db, TODAY + timedelta(days=30)) == []
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_draft(db, Draft("P01", "nudge", "s", "b"), run_id=None)


def test_stop_never_reaches_the_model(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    before = len(llm.calls)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: Example Hackathon",
        "Please remove me from your list and stop contacting me.",
        message_id="<r2>", today=TODAY)
    assert event.label == "declined"
    assert event.confidence == 1.0
    assert len(llm.calls) == before, "the stop rule must not depend on a model call"
    assert store.is_blocked(db, "devi@example.com")
    row = db.one("SELECT payload FROM actions WHERE kind='reply_declined'")
    assert json.loads(row["payload"])["rule"] == "explicit opt-out"


def test_out_of_office_pushes_seven_days_and_costs_no_touch(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Automatic reply: out of office",
        "I am out of office until the 5th with limited access to email.",
        message_id="<r3>", today=TODAY)
    assert event.label == "out_of_office"
    after = store.get(db, "P01")
    assert after.touches == 1
    assert after.next_touch == (TODAY + timedelta(days=7)).isoformat()
    assert after.status == "sent"


def test_out_of_office_subject_rule_skips_the_model(cfg, db, llm, prospect):
    before = len(llm.calls)
    verdict = replies.classify(llm, cfg, db, "devi@example.com",
                               "Automatic reply: Re: Example Hackathon",
                               "I will respond when I return.")
    assert verdict["label"] == "out_of_office"
    assert verdict["rule"] == "auto-reply subject"
    assert len(llm.calls) == before


def test_interested_stops_the_ladder_for_a_human(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: Example Hackathon",
        "This sounds good, can you send the deck and set up a call?",
        message_id="<r4>", today=TODAY)
    assert event.label == "interested"
    after = store.get(db, "P01")
    assert after.status == "interested"
    assert after.next_touch is None
    assert store.due_today(db, TODAY + timedelta(days=30)) == []
    row = db.one("SELECT escalated FROM actions WHERE kind='reply_interested'")
    assert row["escalated"] == 1


def test_needs_info_sets_replied_and_clears_next_touch(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: Example Hackathon",
        "What is the split between first and final year students?",
        message_id="<r5>", today=TODAY)
    assert event.label == "needs_info"
    assert event.extracted_ask
    after = store.get(db, "P01")
    assert after.status == "replied"
    assert after.next_touch is None
    assert after.touches == 1


def test_classify_uses_the_cheap_model_at_temperature_zero(cfg, db, llm, prospect):
    verdict = replies.classify(llm, cfg, db, "devi@example.com", "Re",
                               "Can you send the deck?")
    assert verdict["rule"] == "model"
    assert llm.calls[-1]["model"] == cfg.resolved_models()[1]
    assert llm.calls[-1]["system"].startswith("TASK: classify")
    row = db.one("SELECT 1 FROM actions WHERE kind='classify_reply'")
    assert row is not None


def test_unknown_label_from_the_model_becomes_other(cfg, db, prospect):
    scripted = FakeLLM(cfg, scripted=[LLMResponse(
        text='{"label": "maybe", "confidence": "0.55", "extracted_ask": ""}')])
    verdict = replies.classify(scripted, cfg, db, "devi@example.com", "Re", "hm")
    assert verdict["label"] == "other"
    assert verdict["confidence"] == 0.55
    assert verdict["extracted_ask"] is None


def test_a_reply_is_only_handled_once(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(llm, cfg, db, sent, sent.email, "Re",
                                       "ok thanks", message_id="<dup>", today=TODAY)
    assert event.label == "other"
    assert store.seen_message(db, "<dup>")
    assert not store.seen_message(db, "<never-seen>")
    assert store.get(db, "P01").status == "sent", "other leaves the ladder alone"


def test_thread_matching_falls_back_to_the_address(db, prospect):
    assert store.match_thread(db, ["<unknown>"], "devi@example.com") == "P01"
    assert store.match_thread(db, [], "Devi@Example.com ") == "P01"
    assert store.match_thread(db, [], "someone@else.com") is None


def test_a_reply_cancels_the_draft_still_waiting_in_the_queue(cfg, db, llm, prospect):
    draft_id = store.insert_draft(db, Draft("P01", "initial", "hello", "body"),
                                  run_id=None)
    store.set_status(db, "P01", "queued")
    assert store.has_pending_draft(db, "P01")
    replies.classify_and_apply(
        llm, cfg, db, store.get(db, "P01"), prospect.email, "Re",
        "This sounds good, send the deck and lets set up a call.",
        message_id="<r9>", today=TODAY)
    assert store.get(db, "P01").status == "interested"
    assert store.pending_drafts(db) == []
    assert store.get_draft(db, draft_id)["verdict"] == "skipped"


def test_a_declined_prospect_is_not_reopened_by_a_later_reply(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    replies.classify_and_apply(llm, cfg, db, sent, sent.email, "Re",
                               "Unfortunately we will pass.", message_id="<d1>",
                               today=TODAY)
    event = replies.classify_and_apply(
        llm, cfg, db, store.get(db, "P01"), sent.email, "Re",
        "Actually, lets talk. Send the deck?", message_id="<d2>", today=TODAY)
    assert event.label == "interested"          # recorded, and a human sees it
    assert store.get(db, "P01").status == "declined"
    assert "permanent" in event.action
    assert db.one("SELECT 1 FROM replies WHERE message_id='<d2>'") is not None


# ---------------------------------------------------------------------------
# message parsing
# ---------------------------------------------------------------------------

def test_body_text_strips_quoted_history():
    msg = EmailMessage()
    msg["From"] = "devi@example.com"
    msg["Subject"] = "Re: Example Hackathon"
    msg.set_content(
        "Sounds good, send the deck over and we can find a slot next week.\n\n"
        "On Tue, 2 Sep 2026 at 10:00, Rudraksh <club@example.org> wrote:\n"
        "> Hi Devi,\n> Quick one about Example Hackathon.\n> Thanks,\n> Rudraksh\n")
    msg.add_alternative("<p>Sounds good, <b>send the deck</b></p>", subtype="html")
    text = replies.body_text(msg)
    assert text.startswith("Sounds good, send the deck over")
    assert "wrote:" not in text
    assert "Quick one" not in text
    assert "<p>" not in text


def test_body_text_is_capped_and_keeps_a_quote_only_reply():
    long = EmailMessage()
    long.set_content("word " * 1000)
    assert len(replies.body_text(long)) == 2000

    quote_only = EmailMessage()
    quote_only.set_content("On Tue, Rudraksh wrote:\n\nsee below, fine by us\n")
    assert "fine by us" in replies.body_text(quote_only)


def test_references_of_collects_both_headers():
    msg = EmailMessage()
    msg["In-Reply-To"] = "<a@example.org>"
    msg["References"] = "<b@example.org> <c@example.org> <a@example.org>"
    assert replies.references_of(msg) == ["<a@example.org>", "<b@example.org>",
                                          "<c@example.org>"]
    assert replies.references_of(EmailMessage()) == []


# ---------------------------------------------------------------------------
# poll: Gmail API
# ---------------------------------------------------------------------------

def test_poll_does_nothing_when_no_provider(cfg, db, llm, prospect, monkeypatch):
    assert cfg.resolved_replies_provider() == "none"
    monkeypatch.setattr(replies, "_gmail_client",
                        lambda cfg: pytest.fail("must not build a client"))
    assert replies.poll(llm, cfg, db, today=TODAY) == []


def test_poll_gmail_handles_each_mail_once(cfg, db, llm, prospect, monkeypatch):
    RawMail, _ = _gmail_names(monkeypatch)
    cfg.replies_provider = "gmail_api"
    db.execute("UPDATE prospects SET status='sent', touches=1, first_touch=?, "
               "gmail_thread_id='T-42' WHERE id='P01'", (TODAY.isoformat(),))
    db.memory_set("sponsorship", "gmail_seen", json.dumps(["u1"]))
    mails = [
        RawMail(uid="u1", message_id="<m1@example.com>", from_addr="devi@example.com",
                subject="Re: Example Hackathon", body="Send the deck, lets do a call.",
                refs=[], thread_id=None, received_at=None),
        RawMail(uid="u2", message_id="<m2@corp.example.com>",
                from_addr="devi.sharma@corp.example.com",
                subject="Re: Example Hackathon",
                body="What is the split between first and final year students?",
                refs=[], thread_id="T-42", received_at=None),
        RawMail(uid="u3", message_id="<m3@stranger.example.com>",
                from_addr="stranger@else.example.com", subject="hello",
                body="Send the deck, lets do a call.", refs=[], thread_id=None,
                received_at=None),
    ]
    client = FakeGmail(mails)
    monkeypatch.setattr(replies, "_gmail_client", lambda cfg: client)

    events = replies.poll(llm, cfg, db, today=TODAY, run_id=None)

    assert client.authorized is False, "polling must never start an OAuth flow"
    assert client.asked == (TODAY - timedelta(days=30), 40)
    assert len(events) == 1
    assert events[0].prospect_id == "P01"
    assert events[0].label == "needs_info"
    assert events[0].message_id == "<m2@corp.example.com>"
    assert store.get(db, "P01").status == "replied"
    assert json.loads(db.memory_get("sponsorship", "gmail_seen")) == ["u1", "u2", "u3"]
    assert db.one("SELECT uid FROM replies WHERE message_id='<m2@corp.example.com>'"
                  )["uid"] == "u2"
    # a second poll with the same mailbox does nothing
    assert replies.poll(llm, cfg, db, today=TODAY) == []


def test_poll_gmail_one_failing_mail_does_not_stop_the_rest(cfg, db, llm, prospect,
                                                            monkeypatch):
    RawMail, _ = _gmail_names(monkeypatch)
    cfg.replies_provider = "gmail_api"
    _sent_prospect(db)
    mails = [RawMail(uid="a", message_id="<a@x>", from_addr="devi@example.com",
                     subject="Re", body="one", refs=[], thread_id=None,
                     received_at=None),
             RawMail(uid="b", message_id="<b@x>", from_addr="devi@example.com",
                     subject="Re", body="Send the deck?", refs=[], thread_id=None,
                     received_at=None)]
    monkeypatch.setattr(replies, "_gmail_client", lambda cfg: FakeGmail(mails))
    real = replies.classify_and_apply
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model hiccup")
        return real(*args, **kwargs)

    monkeypatch.setattr(replies, "classify_and_apply", flaky)
    events = replies.poll(llm, cfg, db, today=TODAY, run_id=None)
    assert [e.message_id for e in events] == ["<b@x>"]
    err = db.one("SELECT target, payload, escalated FROM actions WHERE kind='reply_error'")
    assert err["target"] == "P01" and err["escalated"] == 1
    assert json.loads(err["payload"])["message_id"] == "<a@x>"
    assert json.loads(db.memory_get("sponsorship", "gmail_seen")) == ["a", "b"]


def test_poll_gmail_seen_list_is_capped(cfg, db, llm, prospect, monkeypatch):
    RawMail, _ = _gmail_names(monkeypatch)
    cfg.replies_provider = "gmail_api"
    db.memory_set("sponsorship", "gmail_seen",
                  json.dumps(["x{}".format(i) for i in range(replies.SEEN_CAP)]))
    mails = [RawMail(uid="new", message_id="<n@x>", from_addr="nobody@else.com",
                     subject="spam", body="buy now", refs=[], thread_id=None,
                     received_at=None)]
    monkeypatch.setattr(replies, "_gmail_client", lambda cfg: FakeGmail(mails))
    assert replies.poll(llm, cfg, db, today=TODAY) == []
    seen = json.loads(db.memory_get("sponsorship", "gmail_seen"))
    assert len(seen) == replies.SEEN_CAP
    assert seen[-1] == "new" and "x0" not in seen


def test_poll_gmail_not_configured_returns_empty(cfg, db, llm, prospect, monkeypatch,
                                                 capsys):
    _, GmailNotConfigured = _gmail_names(monkeypatch)
    cfg.replies_provider = "gmail_api"

    def no_client(cfg):
        raise GmailNotConfigured("no token at data/gmail_token.json")

    monkeypatch.setattr(replies, "_gmail_client", no_client)
    assert replies.poll(llm, cfg, db, today=TODAY) == []
    out = capsys.readouterr().out
    assert "gmail-auth" in out and out.count("\n") == 1
    assert db.memory_get("sponsorship", "gmail_seen") is None


# ---------------------------------------------------------------------------
# poll: IMAP
# ---------------------------------------------------------------------------

@pytest.fixture
def imap_cfg(cfg):
    cfg.replies_provider = "imap"
    cfg.imap_host = "imap.example.com"
    cfg.imap_user = "club@example.org"
    cfg.imap_password = "not-a-real-password"
    cfg.imap_folder = "INBOX"
    return cfg


def test_poll_imap_reads_one_reply_and_remembers_the_uid(imap_cfg, db, llm, prospect,
                                                         monkeypatch):
    cfg = imap_cfg
    db.execute("UPDATE prospects SET status='sent', touches=1, first_touch=?, "
               "thread_id='<our-1@example.org>' WHERE id='P01'", (TODAY.isoformat(),))
    FakeIMAP.instances = []
    FakeIMAP.mailbox = {7: _rfc822(
        "devi@example.com", "Re: Example Hackathon",
        "This sounds good, send the deck and let's set up a call.\n\n"
        "On Thu, Rudraksh wrote:\n> Hi Devi,\n> Quick one.\n",
        "<reply-1@example.com>", in_reply_to="<our-1@example.org>")}
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)

    events = replies.poll(llm, cfg, db, today=TODAY, run_id=None)

    assert len(events) == 1
    assert events[0].label == "interested"
    assert events[0].message_id == "<reply-1@example.com>"
    assert "Quick one" not in events[0].snippet
    assert store.get(db, "P01").status == "interested"
    assert db.memory_get("sponsorship", "imap_last_uid") == "7"
    conn = FakeIMAP.instances[0]
    assert conn.host == "imap.example.com"
    assert ("login", "club@example.org") in conn.ops
    assert ("select", "INBOX", True) in conn.ops, "the mailbox must be read-only"
    assert ("uid", "SEARCH", None, "(SINCE 06-Aug-2026)") in conn.ops
    assert conn.ops[-1] == ("logout",)


def test_poll_imap_matches_by_address_when_headers_are_missing(imap_cfg, db, llm,
                                                               prospect, monkeypatch):
    _sent_prospect(db)
    FakeIMAP.mailbox = {3: _rfc822("devi@example.com", "hello",
                                   "Unfortunately no budget this year.",
                                   "<reply-3@example.com>")}
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    events = replies.poll(llm, imap_cfg, db, today=TODAY)
    assert [e.label for e in events] == ["declined"]
    assert store.is_blocked(db, "devi@example.com")


def test_poll_imap_skips_uids_already_seen(imap_cfg, db, llm, prospect, monkeypatch):
    _sent_prospect(db)
    db.memory_set("sponsorship", "imap_last_uid", "7")
    FakeIMAP.instances = []
    FakeIMAP.mailbox = {5: _rfc822("devi@example.com", "Re", "Send the deck?",
                                   "<old@example.com>"),
                        7: _rfc822("devi@example.com", "Re", "Send the deck?",
                                   "<older@example.com>")}
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    assert replies.poll(llm, imap_cfg, db, today=TODAY) == []
    conn = FakeIMAP.instances[0]
    assert not [op for op in conn.ops if op[:2] == ("uid", "FETCH")]
    assert db.memory_get("sponsorship", "imap_last_uid") == "7"
    assert store.get(db, "P01").status == "sent"


def test_imap_fetch_builds_rawmail_from_the_message(imap_cfg, monkeypatch):
    FakeIMAP.mailbox = {9: _rfc822(
        "devi@example.com", "=?utf-8?q?Re=3A_Example_Hackathon?=", "Sure, call me.",
        "<r9@example.com>", in_reply_to="<our-1@example.org>",
        references="<our-0@example.org> <our-1@example.org>")}
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    mails = replies.imap_fetch(imap_cfg, TODAY - timedelta(days=30), limit=40,
                               last_uid=0)
    assert len(mails) == 1
    mail = mails[0]
    assert (mail.uid, mail.message_id, mail.from_addr) == (
        "9", "<r9@example.com>", "devi@example.com")
    assert mail.subject == "Re: Example Hackathon"
    assert mail.body == "Sure, call me."
    assert mail.refs == ["<our-1@example.org>", "<our-0@example.org>"]
    assert mail.thread_id is None
    assert mail.received_at.startswith("2026-09-04")
