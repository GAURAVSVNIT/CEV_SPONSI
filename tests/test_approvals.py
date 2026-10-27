"""The approval gate: the only path from a draft to an email.

Everything here is about the tap. Nothing in this suite touches the network:
delivery is a fake that records what it was asked to send, and Telegram is a
notifier whose `_post` is a list of dicts. What is being asserted is that the
database says the right thing afterwards -- because the database, not the
prompt, is what stops an email going out twice or going out at all.
"""

from __future__ import annotations

import sys
import types
from datetime import date, timedelta

import pytest

from sponsy import approvals, approve, store
from sponsy.models import Draft
from sponsy.telegram import TelegramNotifier

from .conftest import TODAY


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeTelegram(TelegramNotifier):
    """The real notifier with the wire replaced by a list."""

    def __init__(self, chat_id="12345", updates=None):
        super().__init__("token", chat_id, logfile=None)
        self.posts: list[tuple[str, dict]] = []
        self._updates = list(updates or [])
        self._next_id = 100

    def _post(self, method: str, payload: dict) -> dict:
        self.posts.append((method, payload))
        self._next_id += 1
        return {"message_id": self._next_id, "ok": True}

    def get_updates(self, offset=None, timeout=0):
        out = [u for u in self._updates
               if offset is None or u["update_id"] >= offset]
        return out

    def calls(self, method: str) -> list[dict]:
        return [payload for name, payload in self.posts if name == method]


@pytest.fixture
def sent_mail(monkeypatch, tmp_path):
    """Install a fake `deliver` and hand back the list of what it was asked to
    send. sponsy.mail is imported lazily by approve._send, so it has to exist
    in sys.modules under that name."""
    record: list[dict] = []

    def fake_deliver(cfg, event, voice, prospect, subject, body, kind, today=None,
                     in_reply_to=None, thread_id=None, **_):
        record.append({"to": prospect.email, "subject": subject, "body": body,
                       "kind": kind, "in_reply_to": in_reply_to,
                       "thread_id": thread_id, "today": today})
        return {"message_id": "<m{}@example.org>".format(len(record)),
                "channel": "smtp", "eml": str(tmp_path / "x.eml"),
                "mailto": "mailto:" + (prospect.email or ""), "thread_id": None,
                "copied": False}

    try:
        import sponsy.mail as mail_mod
        monkeypatch.setattr(mail_mod, "deliver", fake_deliver)
    except ImportError:                     # pragma: no cover - mail/ is present
        import sponsy
        mail_mod = types.ModuleType("sponsy.mail")
        mail_mod.deliver = fake_deliver
        monkeypatch.setitem(sys.modules, "sponsy.mail", mail_mod)
        monkeypatch.setattr(sponsy, "mail", mail_mod, raising=False)
    return record


@pytest.fixture
def card(db, prospect, notifier, event, voice):
    """A queued draft with a card posted for it, exactly as the job leaves it."""
    approve.register(event, voice, TODAY)
    draft_id = store.insert_draft(
        db, Draft(prospect_id="P01", kind="initial",
                  subject="Example Hackathon, Gold tier",
                  body="Hi Devi,\n\nAcme Analytics runs a student programme.\n\n"
                       "Our Gold tier is Rs 20,000.\n\nThanks,\nRudraksh",
                  claims=["Acme Analytics runs a student programme"],
                  ai_tell_score=0.95),
        run_id=None, action_id=db.log_action(None, "draft_email", "P01", {}))
    message_id = notifier.send(approve.draft_message(store.get_draft(db, draft_id)),
                               buttons=approve.buttons(draft_id))
    store.set_tg_message(db, draft_id, message_id)
    return draft_id


def _verdict(cfg, db, notifier, draft_id, verdict, text=""):
    return approvals.apply_verdict(cfg, db, notifier,
                                   "d:{}:{}".format(draft_id, verdict), text)


# ---------------------------------------------------------------------------
# buttons and cards
# ---------------------------------------------------------------------------

def test_the_five_buttons_are_there_with_their_callback_data():
    rows = approve.buttons(7)
    labels = [label for row in rows for label, _ in row]
    data = [payload for row in rows for _, payload in row]
    assert len(labels) == 5
    assert data == ["d:7:send", "d:7:edit", "d:7:redo", "d:7:skip", "d:7:never"]
    assert all(any(c in label for c in "SEdRkN") for label in labels)


def test_the_card_shows_the_email_and_how_it_scored(db, card):
    text = approve.draft_message(store.get_draft(db, card))
    assert "Acme Analytics" in text and "P01" in text
    assert "devi@example.com" in text
    assert "Example Hackathon, Gold tier" in text
    assert "human-score 0.95" in text
    assert "claims traced to" in text


def test_an_unverified_card_says_what_failed(db, prospect):
    detail = {"checks": {"length": True, "claims_traceable": False},
              "claims": [{"claim": "they sponsored 47 hackathons", "grounded": False}],
              "ungrounded": ["they sponsored 47 hackathons"], "words": 40,
              "sources": 2, "lint": {"issues": []}, "soft_failures": []}
    draft_id = store.insert_draft(db, Draft("P01", "initial", "s", "b"), run_id=None)
    text = approve.draft_message(store.get_draft(db, draft_id), detail)
    assert "UNVERIFIED" in text and "claims_traceable" in text
    assert "ungrounded: they sponsored 47 hackathons" in text


def test_a_stiff_card_says_so_without_calling_it_unverified(db, prospect):
    detail = {"checks": {"length": True}, "claims": [], "ungrounded": [],
              "words": 40, "sources": 1, "soft_failures": ["sounds_human"],
              "lint": {"score": 0.55, "issues": ["Drop the exclamation mark."]}}
    draft_id = store.insert_draft(db, Draft("P01", "initial", "s", "b"), run_id=None)
    text = approve.draft_message(store.get_draft(db, draft_id), detail)
    assert "UNVERIFIED" not in text
    assert "reads a bit robotic" in text and "exclamation" in text


# ---------------------------------------------------------------------------
# [Send]
# ---------------------------------------------------------------------------

def test_send_delivers_marks_and_advances_the_ladder(cfg, db, notifier, card,
                                                     sent_mail):
    toast = _verdict(cfg, db, notifier, card, "send")
    assert "sent via smtp" in toast
    assert len(sent_mail) == 1 and sent_mail[0]["to"] == "devi@example.com"

    row = store.get_draft(db, card)
    assert row["sent_at"] and row["verdict"] == "approved"
    assert row["channel"] == "smtp" and row["message_id"] == "<m1@example.org>"
    after = store.get(db, "P01")
    assert after.touches == 1 and after.status == "sent"
    assert after.next_touch == (TODAY + timedelta(days=3)).isoformat()


def test_send_rewrites_the_card_and_takes_the_buttons_away(cfg, db, notifier, card,
                                                           sent_mail):
    _verdict(cfg, db, notifier, card, "send")
    assert notifier.edited, "the card should have been edited in place"
    last = notifier.edited[-1]
    assert "Sent" in last["text"] and "touch 1/4" in last["text"]
    assert not last["buttons"], "a sent card must not still be tappable"


def test_the_send_is_written_to_the_audit_log(cfg, db, notifier, card, sent_mail):
    _verdict(cfg, db, notifier, card, "send")
    row = db.one("SELECT payload FROM actions WHERE kind='send_email'")
    assert row and "smtp" in row["payload"]
    verdicts = db.query("SELECT human_verdict FROM actions WHERE kind='draft_email'")
    assert verdicts[0]["human_verdict"] == "approved"


def test_a_second_send_does_nothing(cfg, db, notifier, card, sent_mail):
    _verdict(cfg, db, notifier, card, "send")
    toast = _verdict(cfg, db, notifier, card, "send")
    assert toast == "already sent"
    assert len(sent_mail) == 1
    assert store.get(db, "P01").touches == 1


def test_send_refuses_a_blocked_address(cfg, db, notifier, card, sent_mail):
    store.block_email(db, "devi@example.com", "asked us to stop")
    toast = _verdict(cfg, db, notifier, card, "send")
    assert "do-not-contact" in toast
    assert sent_mail == []
    assert store.get_draft(db, card)["sent_at"] is None


def test_send_refuses_a_stale_card(cfg, db, notifier, card, sent_mail):
    """They replied while the card was still sitting in the chat."""
    db.execute("UPDATE prospects SET status='interested' WHERE id='P01'")
    toast = _verdict(cfg, db, notifier, card, "send")
    assert "stale card" in toast
    assert sent_mail == []
    assert store.get_draft(db, card)["sent_at"] is None
    assert store.get(db, "P01").touches == 0


def test_send_refuses_a_superseded_card(cfg, db, notifier, card, sent_mail):
    store.supersede_draft(db, card)
    toast = _verdict(cfg, db, notifier, card, "send")
    assert "superseded" in toast
    assert sent_mail == []


def test_a_needs_info_reply_is_threaded(cfg, db, notifier, prospect, event, voice,
                                        sent_mail):
    approve.register(event, voice, TODAY)
    db.execute("UPDATE prospects SET thread_id='<their-msg@acme.com>', "
               "status='replied' WHERE id='P01'")
    draft_id = store.insert_draft(db, Draft("P01", "needs_info", "Re: sponsorship",
                                            "Hi Devi,\n\nAnswer.\n\nThanks"),
                                  run_id=None)
    _verdict(cfg, db, notifier, draft_id, "send")
    assert sent_mail[0]["in_reply_to"] == "<their-msg@acme.com>"
    # answering a question is not a cold touch
    assert store.get(db, "P01").touches == 0


def test_a_provider_failure_still_records_the_send(cfg, db, notifier, card,
                                                   monkeypatch, tmp_path):
    """You already approved it. A dead mail server must not lose it, and the
    card must say plainly what happened."""
    import sponsy.mail as mail_mod

    def failing(cfg_, event, voice, prospect, subject, body, kind, **kwargs):
        return {"message_id": "<m@example.org>", "channel": "manual",
                "eml": str(tmp_path / "x.eml"), "mailto": "mailto:x",
                "thread_id": None, "copied": False,
                "error": "OSError: connection refused"}

    monkeypatch.setattr(mail_mod, "deliver", failing)
    toast = _verdict(cfg, db, notifier, card, "send")
    assert "provider failed" in toast
    assert "connection refused" in notifier.edited[-1]["text"]
    assert store.get_draft(db, card)["sent_at"], "the send must still be recorded"


# ---------------------------------------------------------------------------
# [Edit]
# ---------------------------------------------------------------------------

def test_edit_records_the_correction_and_sends_your_version(cfg, db, notifier,
                                                            card, sent_mail):
    toast = _verdict(cfg, db, notifier, card, "edit",
                     text="Subject: shorter one\nHi Devi,\n\nOne line instead.")
    assert "sent" in toast
    row = store.get_draft(db, card)
    assert row["verdict"] == "edited"
    assert row["subject"] == "shorter one"
    assert sent_mail[0]["subject"] == "shorter one"
    assert sent_mail[0]["body"] == "Hi Devi,\n\nOne line instead."
    correction = db.one("SELECT * FROM corrections WHERE draft_id=?", (card,))
    assert correction["before_body"] != correction["after_body"]
    assert store.get(db, "P01").touches == 1


def test_edit_without_a_subject_line_keeps_the_old_subject(cfg, db, notifier, card,
                                                           sent_mail):
    _verdict(cfg, db, notifier, card, "edit", text="Hi Devi,\n\nJust the body.")
    assert store.get_draft(db, card)["subject"] == "Example Hackathon, Gold tier"


def test_an_empty_correction_changes_nothing(cfg, db, notifier, card, sent_mail):
    toast = _verdict(cfg, db, notifier, card, "edit", text="Subject: only this\n")
    assert "empty correction" in toast
    assert sent_mail == []
    assert store.get_draft(db, card)["verdict"] is None


def test_edit_without_text_asks_for_it(cfg, db, notifier, card, sent_mail):
    toast = _verdict(cfg, db, notifier, card, "edit")
    assert "send me the text" in toast
    assert sent_mail == []
    assert approvals.pop_pending_edit(db, cfg.telegram_chat_id) == \
        "d:{}:edit".format(card)


# ---------------------------------------------------------------------------
# [Redo]
# ---------------------------------------------------------------------------

@pytest.fixture
def regenerate(db):
    """Stands in for the job: writes a new draft and returns its id."""
    seen: list[tuple[int, str]] = []

    def _regenerate(row, instruction):
        seen.append((row["id"], instruction))
        return store.insert_draft(
            db, Draft(row["prospect_id"], row["kind"], "a quieter subject",
                      "Hi Devi,\n\nShorter.\n\nThanks"), run_id=None)

    _regenerate.seen = seen
    return _regenerate


def test_redo_posts_a_new_draft_and_retires_the_old_one(cfg, db, notifier, card,
                                                        event, voice, regenerate):
    approve.register(event, voice, TODAY, regenerate=regenerate)
    toast = _verdict(cfg, db, notifier, card, "redo", text="shorter, one question")
    assert "new draft posted as d:" in toast
    assert regenerate.seen == [(card, "shorter, one question")]

    old = store.get_draft(db, card)
    assert old["verdict"] == "superseded"
    pending = store.pending_drafts(db)
    assert len(pending) == 1 and pending[0]["supersedes"] == card
    assert "superseded by d:" in notifier.edited[-1]["text"]


def test_redo_without_an_instruction_changes_nothing(cfg, db, notifier, card,
                                                     event, voice, regenerate):
    approve.register(event, voice, TODAY, regenerate=regenerate)
    toast = _verdict(cfg, db, notifier, card, "redo")
    assert "send me the text" in toast
    assert regenerate.seen == []
    assert store.get_draft(db, card)["verdict"] is None


def test_redo_survives_a_broken_regenerator(cfg, db, notifier, card, event, voice):
    def boom(row, instruction):
        raise RuntimeError("model exploded")

    approve.register(event, voice, TODAY, regenerate=boom)
    toast = _verdict(cfg, db, notifier, card, "redo", text="shorter")
    assert "could not regenerate" in toast
    assert store.get_draft(db, card)["verdict"] is None, "the card stays tappable"


def test_redo_is_refused_when_the_job_did_not_offer_it(cfg, db, notifier, card,
                                                       event, voice):
    approve.register(event, voice, TODAY, regenerate=None)
    assert "not available" in _verdict(cfg, db, notifier, card, "redo", text="x")


# ---------------------------------------------------------------------------
# [Skip] and [Never]
# ---------------------------------------------------------------------------

def test_skip_parks_the_prospect_for_three_days(cfg, db, notifier, card, sent_mail):
    toast = _verdict(cfg, db, notifier, card, "skip")
    assert "back on" in toast
    assert store.get_draft(db, card)["verdict"] == "skipped"
    after = store.get(db, "P01")
    assert after.touches == 0
    assert after.next_touch == (TODAY + timedelta(days=3)).isoformat()
    assert sent_mail == []
    assert "skipped" in notifier.edited[-1]["text"]


def test_skipping_twice_is_harmless(cfg, db, notifier, card):
    _verdict(cfg, db, notifier, card, "skip")
    assert "already skipped" in _verdict(cfg, db, notifier, card, "skip")


def test_never_declines_permanently_and_blocks_the_address(cfg, db, notifier, card,
                                                           sent_mail):
    toast = _verdict(cfg, db, notifier, card, "never")
    assert "declined permanently" in toast
    after = store.get(db, "P01")
    assert after.status == "declined"
    assert store.is_blocked(db, "devi@example.com")
    assert store.get_draft(db, card)["verdict"] == "skipped"
    assert store.due_today(db, TODAY + timedelta(days=365)) == []
    assert sent_mail == []


def test_never_is_final_even_from_the_database(cfg, db, notifier, card):
    import sqlite3
    _verdict(cfg, db, notifier, card, "never")
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        db.execute("UPDATE prospects SET status='new' WHERE id='P01'")


def test_a_verdict_on_a_missing_draft_says_so(cfg, db, notifier, prospect, event,
                                              voice):
    approve.register(event, voice, TODAY)
    assert "is gone" in _verdict(cfg, db, notifier, 999, "send")
    assert "is gone" in _verdict(cfg, db, notifier, 999, "skip")


def test_an_unparseable_verdict_is_reported(cfg, db, notifier):
    assert "unrecognised" in approvals.apply_verdict(cfg, db, notifier, "nonsense")


def test_an_unknown_verdict_is_reported(cfg, db, notifier, card, event, voice):
    approve.register(event, voice, TODAY)
    assert "unknown verdict" in _verdict(cfg, db, notifier, card, "explode")


# ---------------------------------------------------------------------------
# Telegram routing
# ---------------------------------------------------------------------------

def _callback(data, chat_id="12345", update_id=1):
    return {"update_id": update_id,
            "callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": 100,
                                           "chat": {"id": int(chat_id)}}}}


def _message(text, chat_id="12345", update_id=1):
    return {"update_id": update_id,
            "message": {"message_id": 101, "text": text,
                        "chat": {"id": int(chat_id)}}}


def test_a_tap_from_another_chat_is_ignored(cfg, db, card, event, voice, sent_mail):
    """Anyone can find a bot. Only one chat may drive it."""
    approve.register(event, voice, TODAY)
    bot = FakeTelegram(chat_id="12345")
    handled = approvals._handle_update(cfg, db, bot,
                                       _callback("d:{}:send".format(card),
                                                 chat_id="99999"))
    assert handled is False
    assert sent_mail == []
    assert store.get_draft(db, card)["sent_at"] is None
    assert bot.calls("answerCallbackQuery")[0]["text"] == "not your bot"


def test_a_tap_from_the_right_chat_is_applied(cfg, db, card, event, voice,
                                              sent_mail):
    approve.register(event, voice, TODAY)
    bot = FakeTelegram(chat_id="12345")
    assert approvals._handle_update(cfg, db, bot,
                                    _callback("d:{}:send".format(card)))
    assert len(sent_mail) == 1
    assert store.get_draft(db, card)["sent_at"]


def test_edit_asks_with_a_force_reply_then_applies_the_next_message(
        cfg, db, card, event, voice, sent_mail):
    approve.register(event, voice, TODAY)
    bot = FakeTelegram(chat_id="12345")
    approvals._handle_update(cfg, db, bot, _callback("d:{}:edit".format(card)))
    reply = bot.calls("sendMessage")[-1]
    assert reply["reply_markup"]["force_reply"] is True
    assert "corrected email" in reply["text"]

    approvals._handle_update(cfg, db, bot,
                             _message("Subject: from my phone\nHi Devi,\n\nShort."))
    row = store.get_draft(db, card)
    assert row["subject"] == "from my phone" and row["verdict"] == "edited"
    assert sent_mail[0]["body"] == "Hi Devi,\n\nShort."


def test_redo_asks_for_an_instruction_not_an_email(cfg, db, card, event, voice,
                                                   regenerate):
    approve.register(event, voice, TODAY, regenerate=regenerate)
    bot = FakeTelegram(chat_id="12345")
    approvals._handle_update(cfg, db, bot, _callback("d:{}:redo".format(card)))
    assert "what should change" in bot.calls("sendMessage")[-1]["text"]
    approvals._handle_update(cfg, db, bot, _message("drop the second paragraph"))
    assert regenerate.seen == [(card, "drop the second paragraph")]


def test_a_stray_message_gets_a_nudge_not_a_verdict(cfg, db, card, event, voice,
                                                    sent_mail):
    approve.register(event, voice, TODAY)
    bot = FakeTelegram(chat_id="12345")
    assert approvals._handle_update(cfg, db, bot, _message("hello?")) is False
    assert "/help" in bot.calls("sendMessage")[-1]["text"]
    assert sent_mail == []


def test_a_slash_command_is_dispatched(cfg, db, card, event, voice):
    seen = []
    approvals.register_command("pending", lambda ctx, args: seen.append(args)
                               or "two waiting")
    bot = FakeTelegram(chat_id="12345")
    assert approvals._handle_update(cfg, db, bot, _message("/pending"))
    assert seen == [""]
    assert "two waiting" in bot.calls("sendMessage")[-1]["text"]


def test_a_command_addressed_to_the_bot_by_name_still_works(cfg, db, event, voice):
    approvals.register_command("run", lambda ctx, args: "ran " + args)
    bot = FakeTelegram(chat_id="12345")
    approvals._handle_update(cfg, db, bot, _message("/run@sponsy_bot 3"))
    assert "ran 3" in bot.calls("sendMessage")[-1]["text"]


def test_an_unknown_command_says_so(cfg, db, event, voice):
    bot = FakeTelegram(chat_id="12345")
    approvals._handle_update(cfg, db, bot, _message("/nonsense"))
    assert "unknown command" in bot.calls("sendMessage")[-1]["text"]


def test_a_command_that_throws_does_not_kill_the_bot(cfg, db, event, voice):
    def boom(ctx, args):
        raise ValueError("nope")

    approvals.register_command("boom", boom)
    bot = FakeTelegram(chat_id="12345")
    assert approvals._handle_update(cfg, db, bot, _message("/boom"))
    assert "failed" in bot.calls("sendMessage")[-1]["text"]


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------

def test_drain_applies_what_you_tapped_last_night(cfg, db, card, event, voice,
                                                  sent_mail):
    """This is what makes a cron-only deployment work: no server, the verdicts
    are picked up at the start of the next run."""
    approve.register(event, voice, TODAY)
    bot = FakeTelegram(chat_id="12345",
                       updates=[_callback("d:{}:send".format(card), update_id=7)])
    assert approvals.drain(cfg, db, bot) == 1
    assert store.get_draft(db, card)["sent_at"]
    assert db.memory_get("approvals", "update_offset") == "7"


def test_drain_does_not_replay_what_it_already_applied(cfg, db, card, event, voice,
                                                       sent_mail):
    approve.register(event, voice, TODAY)
    update = _callback("d:{}:send".format(card), update_id=7)
    bot = FakeTelegram(chat_id="12345", updates=[update])
    approvals.drain(cfg, db, bot)
    assert approvals.drain(cfg, db, bot) == 0
    assert len(sent_mail) == 1


def test_drain_is_a_no_op_without_a_real_bot(cfg, db, notifier):
    assert approvals.drain(cfg, db, notifier) == 0


def test_drain_survives_telegram_being_down(cfg, db):
    class Broken(FakeTelegram):
        def get_updates(self, offset=None, timeout=0):
            raise RuntimeError("telegram is down")

    assert approvals.drain(cfg, db, Broken()) == 0


def test_a_poison_update_is_skipped_not_retried_forever(cfg, db, event, voice):
    """The offset is written before the update is handled, so a broken one is
    stepped over instead of blocking every later tap."""
    approve.register(event, voice, TODAY)
    bad = {"update_id": 9, "callback_query": {"id": "x", "data": "d:notanint:send",
                                              "message": {"message_id": 1,
                                                          "chat": {"id": 12345}}}}
    bot = FakeTelegram(chat_id="12345", updates=[bad])
    approvals.drain(cfg, db, bot)
    assert db.memory_get("approvals", "update_offset") == "9"


# ---------------------------------------------------------------------------
# the notifier itself
# ---------------------------------------------------------------------------

def test_editing_a_card_clears_its_buttons():
    bot = FakeTelegram()
    bot.edit(100, "done", buttons=None)
    payload = bot.calls("editMessageText")[-1]
    assert payload["reply_markup"] == {"inline_keyboard": []}


def test_an_unchanged_card_is_not_an_error():
    class Unmodified(FakeTelegram):
        def _post(self, method, payload):
            raise RuntimeError("telegram editMessageText: message is not modified")

    assert Unmodified().edit(100, "same text") is False


def test_a_failed_toast_never_breaks_the_verdict():
    class Broken(FakeTelegram):
        def _post(self, method, payload):
            raise RuntimeError("nope")

    Broken().answer_callback("cb1", "done")      # must not raise
