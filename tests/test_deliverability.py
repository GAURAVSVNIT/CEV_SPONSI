"""Deliverability: the outbox's pacing, what a failed send means, bounces,
unsubscribe, the DNS checks and the server runtime. Offline throughout: the
provider is a monkeypatched transmit(), DNS is a fake lookup, the clock is
passed in."""

from __future__ import annotations

import http.client
import smtplib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sponsy import approvals, approve, mail, outbox, replies, runtime, store, unsubscribe, web
from sponsy.mail import bounces, dns, errors, pacing, spamcheck
from sponsy.mail.message import build_message
from sponsy.models import Draft

from .conftest import TODAY

UTC = timezone.utc
MONDAY_10 = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)          # a Monday


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def paced(cfg):
    """A real provider (SMTP), pacing on, a window that is always open."""
    cfg.mail_provider = "smtp"
    cfg.smtp_host, cfg.smtp_user = "smtp.example.com", "club@example.org"
    cfg.smtp_password, cfg.smtp_from = "app-password", "club@example.org"
    cfg.dry_run, cfg.allow_send, cfg.send_only_to = False, True, []
    cfg.send_hours, cfg.send_days, cfg.send_tz = "any", "all", "UTC"
    cfg.send_daily_cap, cfg.send_hourly_cap = 20, 8
    cfg.send_min_gap, cfg.send_jitter = 90, 0
    cfg.send_warmup = False
    cfg.check_mx, cfg.public_url = True, ""
    cfg.bounce_pause_rate, cfg.bounce_pause_min = 0.05, 3
    cfg.followups_in_thread = True
    assert cfg.paced
    return cfg


@pytest.fixture
def wire(monkeypatch):
    """The provider. `wire.fail` is raised instead of sending while set."""
    state = SimpleNamespace(sent=[], fail=None)

    def transmit(cfg, msg, to_addr, thread_id=None):
        if state.fail is not None:
            raise state.fail
        state.sent.append({"to": to_addr, "msg": msg, "bytes": bytes(msg)})
        return {"channel": "smtp", "thread_id": None}

    monkeypatch.setattr(mail, "transmit", transmit)
    monkeypatch.setattr(mail, "to_clipboard", lambda text: False)
    return state


@pytest.fixture
def handlers(event, voice):
    approve.register(event, voice, TODAY)


def add_draft(db, pid="P01", kind="initial", subject="Acme and Example Hackathon",
              body="Hi Devi,\n\nA short note.\n\nThanks"):
    return store.insert_draft(db, Draft(pid, kind, subject, body), run_id=None)


def second_prospect(db):
    return store.add_prospect(db, "Globex", email="ana@globex.example", pid="P02")


def send(cfg, db, notifier, draft_id):
    return approvals.apply_verdict(cfg, db, notifier, "d:{}:send".format(draft_id))


def later(item, seconds=1):
    return outbox._parse(item["not_before"]) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# pacing: pure
# ---------------------------------------------------------------------------

def policy(**kw):
    base = dict(daily_cap=20, hourly_cap=8, min_gap_s=90, jitter_s=0,
                window=(9 * 60, 18 * 60), days=pacing.WEEKDAYS, tz=UTC, warmup=False)
    base.update(kw)
    return pacing.Policy(**base)


def test_the_first_send_of_the_day_goes_now():
    when, why = pacing.next_slot(policy(), [], MONDAY_10)
    assert when == MONDAY_10 and why == ""


def test_sends_are_spaced_in_both_directions():
    p = policy()
    when, why = pacing.next_slot(p, [MONDAY_10 - timedelta(seconds=30)], MONDAY_10)
    assert when == MONDAY_10 + timedelta(seconds=60) and "spacing" in why
    # A send already promised 30 s from now rules out now as well.
    when, _ = pacing.next_slot(p, [MONDAY_10 + timedelta(seconds=30)], MONDAY_10)
    assert when == MONDAY_10 + timedelta(seconds=120)


def test_outside_the_window_it_waits_for_the_next_working_morning():
    saturday = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)
    when, why = pacing.next_slot(policy(), [], saturday)
    assert when == datetime(2026, 9, 14, 9, 0, tzinfo=UTC) and "hours" in why
    evening = MONDAY_10.replace(hour=19)
    assert pacing.next_slot(policy(), [], evening)[0] == datetime(2026, 9, 8, 9, 0,
                                                                  tzinfo=UTC)


def test_the_daily_cap_and_the_warm_up():
    p = policy(warmup=True, warmup_start=3, warmup_step=2, min_gap_s=0)
    today = [MONDAY_10 - timedelta(minutes=m) for m in (5, 10, 15)]
    when, why = pacing.next_slot(p, today, MONDAY_10, first_send=today[-1])
    assert when == datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    assert "daily limit" in why and "warming up" in why
    # A week in, the ramp has reached the cap.
    assert p.cap_on(MONDAY_10.date(), MONDAY_10 - timedelta(days=7)) == 17
    assert p.cap_on(MONDAY_10.date(), MONDAY_10 - timedelta(days=30)) == 20


def test_the_hourly_cap():
    p = policy(hourly_cap=2, min_gap_s=0)
    sent = [MONDAY_10 - timedelta(minutes=40), MONDAY_10 - timedelta(minutes=20)]
    when, why = pacing.next_slot(p, sent, MONDAY_10)
    assert when == MONDAY_10 + timedelta(minutes=20) and "hourly" in why


def test_settings_parse_or_say_why():
    assert pacing.parse_hours("09:30-17:00") == (570, 1020)
    assert pacing.parse_days("mon-fri") == pacing.WEEKDAYS
    assert pacing.parse_days("fri-mon") == frozenset({4, 5, 6, 0})
    assert pacing.parse_tz("+05:30").utcoffset(None) == timedelta(hours=5, minutes=30)
    for fn, bad in ((pacing.parse_hours, "9am"), (pacing.parse_days, "weekdays"),
                    (pacing.parse_tz, "Mars/Olympus")):
        with pytest.raises(ValueError):
            fn(bad)


# ---------------------------------------------------------------------------
# the outbox
# ---------------------------------------------------------------------------

def test_an_approval_sends_at_once_when_a_slot_is_free(paced, db, notifier, prospect,
                                                       handlers, wire):
    did = add_draft(db)
    assert send(paced, db, notifier, did) == "sent via smtp"
    row = store.get_draft(db, did)
    assert row["sent_at"] and row["channel"] == "smtp" and row["verdict"] == "approved"
    assert store.get(db, "P01").touches == 1
    assert store.queued_send(db, did)["state"] == "sent"
    assert [s["to"] for s in wire.sent] == ["devi@example.com"]


def test_a_second_tap_waits_for_its_slot(paced, db, notifier, prospect, handlers, wire,
                                         event, voice):
    send(paced, db, notifier, add_draft(db))
    second_prospect(db)
    did = add_draft(db, "P02", subject="Globex and Example Hackathon")
    said = send(paced, db, notifier, did)
    assert said.startswith("approved; goes out about") and "spacing" in said
    assert not store.get_draft(db, did)["sent_at"] and len(wire.sent) == 1
    # Tapping again does not queue it twice.
    assert send(paced, db, notifier, did).startswith("already approved")
    # The card says when, and has no buttons left to tap.
    assert "It leaves on its own" in (notifier.logfile.read_text(encoding="utf-8"))

    item = store.queued_send(db, did)
    assert outbox.dispatch(paced, db, notifier, event, voice,
                           now=later(item, -30)) == []
    [out] = outbox.dispatch(paced, db, notifier, event, voice, now=later(item))
    assert out["state"] == "sent" and len(wire.sent) == 2


def test_nothing_enters_the_outbox_without_a_verdict(db, prospect):
    did = add_draft(db)
    with pytest.raises(sqlite3.IntegrityError, match="human verdict"):
        db.execute("INSERT INTO outbox (draft_id, prospect_id, to_addr) VALUES (?,?,?)",
                   (did, "P01", "devi@example.com"))


def test_nothing_leaves_the_outbox_for_a_blocked_address(db, prospect):
    did = add_draft(db)
    db.execute("UPDATE drafts SET verdict='approved' WHERE id=?", (did,))
    db.execute("INSERT INTO outbox (draft_id, prospect_id, to_addr) VALUES (?,?,?)",
               (did, "P01", "devi@example.com"))
    store.block_email(db, "Devi@Example.com", "replied stop")
    with pytest.raises(sqlite3.IntegrityError, match="do-not-contact"):
        db.execute("UPDATE outbox SET state='sending' WHERE draft_id=?", (did,))


def test_a_retry_resends_the_same_email(paced, db, notifier, prospect, handlers, wire,
                                       event, voice):
    wire.fail = ConnectionResetError("connection reset by peer")
    did = add_draft(db)
    said = send(paced, db, notifier, did)
    assert "retrying" in said
    item = store.queued_send(db, did)
    assert item["state"] == "queued" and item["attempts"] == 1 and item["message_id"]
    first_bytes = open(item["eml"], "rb").read()

    wire.fail = None
    outbox.dispatch(paced, db, notifier, event, voice, now=later(item))
    assert wire.sent[0]["msg"]["Message-ID"] == item["message_id"]
    assert wire.sent[0]["bytes"].replace(b"\r\n", b"\n") == \
        first_bytes.replace(b"\r\n", b"\n")
    assert store.get_draft(db, did)["sent_at"]


def test_after_the_last_retry_it_falls_back_to_manual(paced, db, notifier, prospect,
                                                      handlers, wire, event, voice):
    wire.fail = OSError("network is unreachable")
    did = add_draft(db)
    send(paced, db, notifier, did)
    for _ in range(outbox.MAX_ATTEMPTS - 1):
        outbox.dispatch(paced, db, notifier, event, voice,
                        now=later(store.queued_send(db, did)))
    row = store.get_draft(db, did)
    assert row["sent_at"] and row["channel"] == "manual"
    assert store.queued_send(db, did)["state"] == "failed"
    assert "Provider failed" in notifier.logfile.read_text(encoding="utf-8")


def test_slow_down_pauses_everything_until_the_provider_said(paced, db, notifier,
                                                            prospect, handlers, wire,
                                                            event, voice):
    wire.fail = smtplib.SMTPDataError(
        421, b"4.7.28 Our system has detected an unusual rate of unsolicited mail")
    did = add_draft(db)
    send(paced, db, notifier, did)
    hold = outbox.paused(db)
    assert hold and hold["until"] and "slow down" in hold["reason"]
    assert store.queued_send(db, did)["state"] == "queued"
    assert "Sending paused" in notifier.logfile.read_text(encoding="utf-8")

    wire.fail = None
    until = outbox._parse(hold["until"])
    assert outbox.dispatch(paced, db, notifier, event, voice,
                           now=until - timedelta(minutes=5)) == []
    outbox.dispatch(paced, db, notifier, event, voice, now=until + timedelta(seconds=1))
    assert store.get_draft(db, did)["sent_at"] and outbox.paused(db) is None


def test_refused_as_spam_pauses_until_a_human_resumes(paced, db, notifier, prospect,
                                                     handlers, wire, event, voice):
    wire.fail = smtplib.SMTPDataError(550, b"5.7.1 Message rejected as spam")
    did = add_draft(db)
    send(paced, db, notifier, did)
    hold = outbox.paused(db)
    assert hold and hold["until"] == ""
    wire.fail = None
    far = datetime.now(UTC) + timedelta(days=3)
    assert outbox.dispatch(paced, db, notifier, event, voice, now=far) == []
    outbox.resume(db)
    outbox.dispatch(paced, db, notifier, event, voice, now=far)
    assert store.get_draft(db, did)["sent_at"]


def test_no_such_user_is_a_bounce(paced, db, notifier, prospect, handlers, wire):
    wire.fail = smtplib.SMTPRecipientsRefused(
        {"devi@example.com": (550, b"5.1.1 The email account does not exist")})
    did = add_draft(db)
    assert send(paced, db, notifier, did).startswith("not sent") or \
        store.queued_send(db, did)["state"] == "failed"
    assert store.queued_send(db, did)["state"] == "failed"
    assert not store.get_draft(db, did)["sent_at"]
    assert store.is_blocked(db, "devi@example.com")
    assert "P01" in store.awaiting_contact(db)
    assert store.due_today(db, TODAY) == []


def test_a_domain_that_takes_no_mail_is_never_sent_to(paced, db, notifier, prospect,
                                                     handlers, wire, monkeypatch):
    paced.offline = False
    monkeypatch.setattr(dns, "cached_mail_domain_status",
                        lambda db, domain, **kw: ("none", "example.com publishes a null MX"))
    did = add_draft(db)
    said = send(paced, db, notifier, did)
    assert said.startswith("not sent: bounce-risk") and wire.sent == []
    assert store.is_blocked(db, "devi@example.com")


def test_a_reply_cancels_what_is_still_queued(paced, db, notifier, prospect, handlers,
                                              wire):
    send(paced, db, notifier, add_draft(db, "P01"))
    second_prospect(db)
    did = add_draft(db, "P02")
    send(paced, db, notifier, did)                     # waits for its slot
    replies.apply_label(db, store.get(db, "P02"), "declined", TODAY)
    item = store.queued_send(db, did)
    assert item["state"] == "cancelled" and "declined" in item["reason"]


def test_one_dispatcher_at_a_time(db):
    now = MONDAY_10
    assert outbox._acquire(db, "serve", now)
    assert not outbox._acquire(db, "web", now)
    assert outbox._acquire(db, "serve", now)               # renewing is fine
    assert outbox._acquire(db, "web", now + outbox.LEASE_TTL + timedelta(seconds=1))


def _stuck(db, did, message_id="<stuck@example.org>"):
    db.execute("UPDATE drafts SET verdict='approved' WHERE id=?", (did,))
    old = outbox._iso(datetime.now(UTC) - timedelta(hours=1))
    db.execute("INSERT INTO outbox (draft_id, prospect_id, to_addr, state, not_before, "
               "message_id, updated_at) VALUES (?,?,?,'sending',?,?,?)",
               (did, "P01", "devi@example.com", old, message_id, old))


def test_an_interrupted_send_is_retried_under_the_same_id(paced, db, prospect):
    did = add_draft(db)
    _stuck(db, did)
    outbox._recover(paced, db, datetime.now(UTC))
    item = store.queued_send(db, did)
    assert item["state"] == "queued" and item["message_id"] == "<stuck@example.org>"


def test_an_interrupted_send_found_in_gmail_sent_is_not_sent_twice(paced, db, prospect,
                                                                 handlers, monkeypatch):
    paced.mail_provider = "gmail_api"

    class Gmail:
        def __init__(self, cfg):
            pass

        def authorize(self, interactive=True):
            return self

        def find_sent(self, message_id):
            return {"id": "g9", "threadId": "t9"}

    monkeypatch.setattr(mail, "GmailClient", Gmail)
    did = add_draft(db)
    _stuck(db, did)
    outbox._recover(paced, db, datetime.now(UTC))
    assert store.queued_send(db, did)["state"] == "sent"
    row = store.get_draft(db, did)
    assert row["sent_at"] and row["message_id"] == "<stuck@example.org>"
    assert store.get(db, "P01").gmail_thread_id == "t9"


def test_too_many_bounces_stop_all_sending(paced, db, prospect):
    now = datetime.now(UTC)
    for i in range(10):
        did = add_draft(db, subject="s{}".format(i))
        db.execute("UPDATE drafts SET verdict='approved', sent_at=?, channel='smtp' "
                   "WHERE id=?", (outbox._iso(now), did))
    assert outbox._bounce_breaker(paced, db, now) == ""
    for i in range(3):
        store.block_email(db, "gone{}@example.net".format(i), "bounce: 5.1.1 no such user")
    assert "3 of the 10" in outbox._bounce_breaker(paced, db, now)
    outbox.resume(db)                       # a human looked: start counting again
    assert outbox._bounce_breaker(paced, db, now + timedelta(seconds=5)) == ""


def test_a_follow_up_replies_in_the_first_emails_thread(paced, db, notifier, prospect,
                                                       handlers, wire, event, voice):
    send(paced, db, notifier, add_draft(db))
    first_id = wire.sent[0]["msg"]["Message-ID"]
    did = add_draft(db, kind="nudge", subject="Re: Acme and Example Hackathon",
                    body="Hi Devi,\n\nOne more thing.\n\nThanks")
    send(paced, db, notifier, did)
    item = store.queued_send(db, did)
    outbox.dispatch(paced, db, notifier, event, voice, now=later(item))
    msg = wire.sent[1]["msg"]
    assert msg["In-Reply-To"] == first_id and first_id in msg["References"]
    assert approve.spam_risks(store.get_draft(db, did)) == []      # an honest Re:
    from sponsy.job import SponsorshipJob
    assert SponsorshipJob._thread_subject("Re: re: Hello") == "Re: Hello"


def test_status_for_the_page(paced, db, notifier, prospect, handlers, wire):
    send(paced, db, notifier, add_draft(db))
    info = outbox.status(paced, db)
    assert info["paced"] and info["sent_today"] == 1 and info["cap_today"] == 20
    assert "max 20/day" in info["policy"]


# ---------------------------------------------------------------------------
# what an error means
# ---------------------------------------------------------------------------

class HttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError."""

    def __init__(self, status, content):
        super().__init__(content)
        self.resp = SimpleNamespace(status=status)
        self.content = content.encode()


@pytest.mark.parametrize("exc, kind", [
    (ConnectionResetError("reset"), "transient"),
    (smtplib.SMTPServerDisconnected("gone"), "transient"),
    (smtplib.SMTPDataError(451, b"4.3.0 temporary local problem"), "transient"),
    (smtplib.SMTPDataError(421, b"4.7.0 Try again later, closing connection"), "throttle"),
    (smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded"), "throttle"),
    (smtplib.SMTPDataError(550, b"5.7.26 Unauthenticated email is not accepted"),
     "reputation"),
    (smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"5.1.1 user unknown")}), "recipient"),
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"),
     "auth"),
    (HttpError(429, "User-rate limit exceeded"), "throttle"),
    (HttpError(403, "Daily Limit Exceeded"), "throttle"),
    (HttpError(503, "backend error"), "transient"),
    (HttpError(400, "Invalid To header"), "recipient"),
    (mail.GmailNotConfigured("token expired"), "auth"),
    (ValueError("no email address to send to"), "permanent"),
])
def test_failures_are_read_for_what_they_mean(exc, kind):
    assert errors.classify(exc).kind == kind


def test_a_daily_quota_pauses_for_a_day():
    f = errors.classify(smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota "
                                                   b"exceeded"))
    assert f.pause == timedelta(hours=24)
    assert errors.retry_after(1) == timedelta(minutes=2)
    assert errors.retry_after(20) == timedelta(minutes=180)


# ---------------------------------------------------------------------------
# bounces
# ---------------------------------------------------------------------------

GMAIL_DSN = """Address not found

Your message wasn't delivered to devi@example.com because the address couldn't
be found, or is unable to receive mail.

The response from the remote server was:
550 5.1.1 The email account that you tried to reach does not exist."""


def test_a_gmail_bounce_is_hard_and_names_the_address():
    b = bounces.parse("mailer-daemon@googlemail.com",
                      "Delivery Status Notification (Failure)", GMAIL_DSN,
                      ours={"club@example.org"})
    assert b.hard and b.status == "5.1.1" and b.recipients == ["devi@example.com"]
    assert "5.1.1" in b.diagnostic


def test_a_delay_notice_is_soft_and_a_person_is_not_a_bounce():
    b = bounces.parse("mailer-daemon@googlemail.com",
                      "Delivery Status Notification (Delay)",
                      "Message delivery to devi@example.com delayed; still trying.")
    assert b is not None and not b.hard
    assert bounces.parse("devi@example.com", "Re: delivery failed?",
                         "Ha, my parcel delivery failed too. Let's talk.") is None


POSTFIX_DSN = """From: MAILER-DAEMON@mx.example.net
To: club@example.org
Subject: Undelivered Mail Returned to Sender
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

This is the mail system at host mx.example.net.
I'm sorry to have to inform you that your message could not be delivered.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.net

Final-Recipient: rfc822; ana@globex.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <ana@globex.example>: Recipient address rejected

--B--
"""


def test_the_imap_path_reads_the_delivery_status_part():
    import email as email_lib
    msg = email_lib.message_from_string(POSTFIX_DSN)
    assert "Final-Recipient" not in replies.body_text(msg)     # not text/plain
    text = "\n".join((replies.body_text(msg), replies.dsn_text(msg)))
    b = bounces.parse(msg["From"], msg["Subject"], text, ours={"club@example.org"})
    assert b.hard and b.status == "5.1.1" and b.recipients == ["ana@globex.example"]


def test_a_bounce_stops_the_ladder_and_gives_the_touch_back(cfg, db, prospect, handlers):
    did = add_draft(db)
    db.execute("UPDATE drafts SET verdict='approved' WHERE id=?", (did,))
    store.mark_sent(db, did, TODAY, message_id="<m1@example.org>", channel="smtp")
    assert store.get(db, "P01").touches == 1
    dsn = replies._RawMail(uid="9", message_id="<dsn@google.com>",
                           from_addr="mailer-daemon@googlemail.com",
                           subject="Delivery Status Notification (Failure)",
                           body=GMAIL_DSN, refs=["<m1@example.org>"])
    # llm=None: a bounce never reaches the model.
    [event] = replies._handle_mails(None, cfg, db, [dsn], TODAY, None,
                                    is_seen=lambda uid: False, remember=lambda uid: None)
    assert event.label == "bounced" and "touch was given back" in event.action
    p = store.get(db, "P01")
    assert p.touches == 0 and p.first_touch is None
    assert store.is_blocked(db, "devi@example.com") and "P01" in store.awaiting_contact(db)
    # A corrected address starts over at touch 1.
    store.set_email(db, "P01", "devrel@example.org")
    assert [x.id for x in store.due_today(db, TODAY)] == ["P01"]


def test_a_quoted_footer_is_not_an_opt_out():
    reply = ("Happy to talk next week, send the deck.\n\n"
             "On Fri, 5 Sep 2026 at 10:00, Rudraksh <club@example.org> wrote:\n"
             "> If this is not for you, reply unsubscribe and we stop.\n")
    kept = replies.strip_quoted(reply)
    assert kept.startswith("Happy to talk") and not replies.STOP.search(kept)


# ---------------------------------------------------------------------------
# unsubscribe
# ---------------------------------------------------------------------------

def test_every_email_carries_a_way_out(cfg, event, voice, prospect):
    cfg.smtp_from = "club@example.org"
    cfg.mail_provider = "smtp"
    msg = build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nBody")
    assert msg["List-Unsubscribe"] == "<mailto:club@example.org?subject=unsubscribe>"
    assert "List-Unsubscribe-Post" not in msg            # one-click needs https
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "we will not write again" in text and "we will not write again" in html

    cfg.public_url = "https://sponsy.example.org"
    msg = build_message(cfg, event, voice, prospect, "s", "Hi Devi,\n\nBody")
    link = unsubscribe.url_for(cfg, "P01")
    assert link.startswith("https://sponsy.example.org/u/P01/")
    assert msg["List-Unsubscribe"].startswith("<{}>, <mailto:".format(link))
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert link in msg.get_body(preferencelist=("plain",)).get_content()


def test_links_are_signed(cfg):
    sig = unsubscribe.signature(cfg, "P01")
    assert unsubscribe.verify(cfg, "P01", sig)
    assert not unsubscribe.verify(cfg, "P02", sig)
    assert not unsubscribe.verify(cfg, "P01", "0" * 32)
    assert not unsubscribe.verify(cfg, "../x", sig)


@pytest.fixture
def public_server(cfg, event, voice):
    cfg.public_url = "https://sponsy.example.org"
    app = web.WebApp(cfg, event, voice, token="s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, httpd.server_port
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


PUBLIC = {"Host": "sponsy.example.org"}


def test_one_click_works_without_a_token_and_a_get_changes_nothing(public_server, cfg,
                                                                  db, prospect):
    app, port = public_server
    path = "/u/P01/" + unsubscribe.signature(cfg, "P01")
    status, page = call(port, "GET", path, headers=PUBLIC)
    assert status == 200 and b"<form" in page
    assert store.get(db, "P01").status == "new"         # link scanners fetch URLs

    status, out = call(port, "POST", path, body="List-Unsubscribe=One-Click",
                       headers=dict(PUBLIC, **{"Content-Type":
                                               "application/x-www-form-urlencoded"}))
    assert status == 200 and out == b"unsubscribed"
    assert store.get(db, "P01").status == "declined"
    assert store.is_blocked(db, "devi@example.com")
    # A provider's retry is harmless.
    assert call(port, "POST", path, body="List-Unsubscribe=One-Click", headers=PUBLIC)[0] == 200


def test_a_forged_link_does_nothing(public_server, db, prospect):
    app, port = public_server
    status, _ = call(port, "POST", "/u/P01/" + "a" * 32, body="List-Unsubscribe=One-Click",
                     headers=PUBLIC)
    assert status == 404 and store.get(db, "P01").status == "new"


def test_the_public_name_never_serves_the_ui_without_a_token(public_server):
    app, port = public_server
    assert call(port, "GET", "/healthz", headers=PUBLIC) == (200, b'{"ok": true}')
    assert call(port, "GET", "/api/overview", headers=PUBLIC)[0] == 401
    app.token = ""
    assert call(port, "GET", "/", headers=PUBLIC)[0] == 403
    assert call(port, "GET", "/api/overview", headers=PUBLIC)[0] == 403


def test_the_page_tells_a_bounce_from_a_stop_and_shows_the_outbox(
        paced, db, notifier, prospect, handlers, wire, event, voice):
    send(paced, db, notifier, add_draft(db))
    second_prospect(db)
    did = add_draft(db, "P02")
    send(paced, db, notifier, did)                     # waits for its slot
    store.bounced(db, "P01", "devi@example.com", "bounce: 5.1.1 no such user")
    app = web.WebApp(paced, event, voice)
    rows = {r["id"]: r for r in app.handle("GET", "/api/prospects")[1]["prospects"]}
    assert rows["P01"]["blocked"] and rows["P01"]["blocked_reason"].startswith("bounce")
    assert rows["P02"]["in_outbox"] == 1 and not rows["P02"]["blocked"]
    detail = app.handle("GET", "/api/prospects/P02")[1]
    [d] = detail["drafts"]
    assert d["send_state"] == "queued" and d["send_note"].startswith("goes out")
    assert app.handle("GET", "/api/prospects/P01")[1]["blocked_reason"].startswith("bounce")


def test_the_page_can_steer_the_outbox(cfg, event, voice, db):
    app = web.WebApp(cfg, event, voice)
    status, out = app.handle("GET", "/api/outbox")
    assert status == 200 and "policy" in out and out["queued"] == []
    assert app.handle("POST", "/api/outbox/pause", {"reason": "checking DNS"})[0] == 200
    assert outbox.paused(db)["reason"] == "checking DNS"
    assert app.handle("GET", "/api/overview")[1]["outbox"]["paused"]
    assert app.handle("POST", "/api/outbox/resume", {})[0] == 200
    assert outbox.paused(db) is None
    assert app.handle("POST", "/api/outbox/7/cancel", {})[0] == 409


# ---------------------------------------------------------------------------
# DNS and doctor
# ---------------------------------------------------------------------------

def fake_dns(table):
    def lookup(name, rtype):
        value = table.get((name, rtype))
        if value is None:
            return dns.Answer("nodata")
        if isinstance(value, str):
            return dns.Answer(value)
        return dns.Answer("ok", list(value))
    return lookup


def test_can_a_domain_receive_mail():
    lookup = fake_dns({("good.example", "MX"): ["10 mx.good.example."],
                       ("null.example", "MX"): ["0 ."],
                       ("gone.example", "MX"): "nxdomain",
                       ("bare.example", "A"): ["192.0.2.1"],
                       ("flaky.example", "MX"): "error"})
    status = lambda d: dns.mail_domain_status(d, lookup=lookup)[0]  # noqa: E731
    assert status("good.example") == "ok"
    assert status("null.example") == "none"
    assert status("gone.example") == "none"
    assert status("bare.example") == "ok"                 # implicit MX
    assert status("nothing.example") == "none"
    assert status("flaky.example") == "unknown"           # never blocks on our trouble


def test_the_answer_is_cached(db):
    calls = []

    def lookup(name, rtype):
        calls.append((name, rtype))
        return dns.Answer("ok", ["10 mx.good.example."])

    assert dns.cached_mail_domain_status(db, "good.example", lookup=lookup)[0] == "ok"
    assert dns.cached_mail_domain_status(db, "good.example", lookup=lookup)[0] == "ok"
    assert len(calls) == 1


GOOD_KEY = "p=" + "A" * 392                 # a 2048-bit key's length in base64


def test_sender_domain_checks(cfg, event):
    from sponsy import doctor
    cfg.mail_provider = "smtp"
    cfg.smtp_user = cfg.smtp_from = "club@club.example"
    bare = fake_dns({("club.example", "MX"): ["1 aspmx.l.google.com."]})
    check = doctor.check_sender_domain(cfg, None, live=True, lookup=bare)
    assert not check.ok and not check.optional and "v=spf1" in check.hint

    full = fake_dns({
        ("club.example", "TXT"): ["v=spf1 include:_spf.google.com ~all"],
        ("google._domainkey.club.example", "TXT"): ["v=DKIM1; k=rsa; " + GOOD_KEY],
        ("_dmarc.club.example", "TXT"): ["v=DMARC1; p=none; rua=mailto:d@club.example"],
        ("club.example", "MX"): ["1 aspmx.l.google.com."]})
    check = doctor.check_sender_domain(cfg, None, live=True, lookup=full)
    assert check.ok, check.detail
    assert "DKIM 'google' (2048-bit)" in check.detail and "p=none" in check.detail

    wrong = fake_dns({("club.example", "TXT"): ["v=spf1 include:mailgun.org ~all"],
                      ("club.example", "MX"): ["1 mx.club.example."]})
    check = doctor.check_sender_domain(cfg, None, live=True, lookup=wrong)
    assert "does not include _spf.google.com" in check.detail

    cfg.smtp_user = cfg.smtp_from = "club@gmail.com"
    assert doctor.check_sender_domain(cfg, None, live=True, lookup=bare).ok


def test_pacing_and_content_checks(cfg, event, voice):
    from sponsy import doctor
    cfg.send_tz = "Mars/Olympus"
    assert not doctor.check_pacing(cfg).ok
    cfg.send_tz, cfg.send_daily_cap = "UTC", 500
    assert "far more than a person" in doctor.check_pacing(cfg).detail
    cfg.smtp_from = "club@example.org"
    check = doctor.check_content(cfg, event, voice)
    assert check.ok, check.detail


# ---------------------------------------------------------------------------
# content warnings
# ---------------------------------------------------------------------------

def test_spam_warnings_on_the_card():
    assert spamcheck.check("A note about Example Hackathon", "Hi,\n\nPlain words.") == []
    risks = spamcheck.check("Re: FREE sponsorship OFFER!!", "Click here: https://bit.ly/x")
    joined = " ".join(risks)
    for bit in ("Re:/Fwd:", "exclamation", "ALL-CAPS", "shortener", "salesy"):
        assert bit in joined
    assert not any("Re:" in r for r in spamcheck.check("Re: hello", "x", threaded=True))


# ---------------------------------------------------------------------------
# the server runtime
# ---------------------------------------------------------------------------

def test_the_daily_run_fires_once_a_day(db):
    daily = runtime.Daily("09:30")
    morning = datetime(2026, 9, 7, 9, 0)
    assert not daily.due(db, morning)
    assert daily.due(db, morning.replace(hour=10))
    daily.mark(db, morning.replace(hour=10))
    assert not daily.due(db, morning.replace(hour=18))
    assert daily.due(db, morning.replace(day=8, hour=10))
    with pytest.raises(ValueError):
        runtime.Daily("9.30am")


def test_backup_and_heartbeat(cfg, db, prospect, tmp_path):
    folder = tmp_path / "backups"
    for _ in range(3):
        path = runtime.backup(cfg, db, keep=2, folder=folder)
    copy = sqlite3.connect(str(path))
    assert copy.execute("SELECT company FROM prospects").fetchone()[0] == "Acme Analytics"
    copy.close()
    assert len(list(folder.glob("sponsy-*.db"))) <= 2
    assert runtime.heartbeat_age(cfg) is None
    runtime.heartbeat(cfg)
    assert runtime.heartbeat_age(cfg) < 5
