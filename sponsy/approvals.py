"""Human-in-the-loop plumbing.

Callback data is "<prefix>:<target>:<verdict>", e.g. "d:12:send". Jobs register
a handler for their prefix; the CLI registers slash-commands. Two ways to
consume what the human did:

  drain()  -- one non-blocking getUpdates pass. The daily job calls this
              first, so a run that fires at 10:00 applies whatever you tapped
              at 23:00 last night. This is what makes a cron-only deployment
              work without a server.
  serve()  -- long-poll loop for instant reactions while you sit with it.

[Edit] and [Redo] need a second message from you. The tap parks the callback
data in `memory` keyed by chat, sends a force-reply, and the next plain text
message from that chat is routed to the edit handler. Only the configured chat
is ever listened to: anyone else who finds the bot gets nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .state import Database
from .telegram import Notifier, TelegramNotifier, esc

JOB = "approvals"
CHUNK = 3500                              # command output is split at this size
TOAST_LEN = 200
Handler = Callable[["Context"], str]
Command = Callable[["Context", str], str]

HANDLERS: dict[str, Handler] = {}
EDIT_HANDLERS: dict[str, Handler] = {}
COMMANDS: dict[str, Command] = {}

# Verdicts that need a follow-up text message before anything happens.
NEEDS_TEXT = ("edit", "redo")
HELP_HINT = "Tap a button on a draft, or /help"


@dataclass
class Context:
    cfg: object
    db: Database
    notifier: Notifier
    prefix: str
    target: str
    verdict: str
    text: str = ""
    callback_id: str = ""
    chat_id: str = ""


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register(prefix: str, handler: Handler, edit_handler: Handler | None = None) -> None:
    HANDLERS[prefix] = handler
    if edit_handler:
        EDIT_HANDLERS[prefix] = edit_handler


def register_command(name: str, fn: Command) -> None:
    COMMANDS[name.strip().lstrip("/").lower()] = fn


def parse_data(data: str) -> tuple[str, str, str] | None:
    parts = (data or "").strip().split(":")
    if len(parts) < 3 or not all(parts[:3]):
        return None
    return parts[0], parts[1], parts[2]


def _dispatch(cfg, db: Database, notifier: Notifier, data: str, text: str = "",
              callback_id: str = "", chat_id: str = "", use_edit: bool = False) -> str:
    parsed = parse_data(data)
    if parsed is None:
        return "unrecognised action: " + (data or "(empty)")
    prefix, target, verdict = parsed
    handler = None
    if use_edit:
        handler = EDIT_HANDLERS.get(prefix)
    handler = handler or HANDLERS.get(prefix)
    if handler is None:
        return "no handler for " + prefix
    ctx = Context(cfg=cfg, db=db, notifier=notifier, prefix=prefix, target=target,
                  verdict=verdict, text=text, callback_id=callback_id,
                  chat_id=chat_id or str(getattr(cfg, "telegram_chat_id", "") or ""))
    return handler(ctx)


def apply_verdict(cfg, db: Database, notifier: Notifier, data: str,
                  text: str = "") -> str:
    """Entry point for the CLI: `python run.py verdict d:12:send`.

    `d:12:edit --text ...` goes straight to the edit handler; without text the
    main handler answers that it needs some, which is the honest reply from a
    terminal where there is no force-reply to wait on."""
    parsed = parse_data(data)
    use_edit = bool(parsed and parsed[2] in NEEDS_TEXT and text.strip())
    return _dispatch(cfg, db, notifier, data, text=text.strip(), use_edit=use_edit)


# ---------------------------------------------------------------------------
# pending-edit state
# ---------------------------------------------------------------------------

def _pending_key(chat_id: str) -> str:
    return "pending_edit:{}".format(chat_id)


def set_pending_edit(db: Database, chat_id: str, data: str) -> None:
    db.memory_set(JOB, _pending_key(str(chat_id)), data)


def pop_pending_edit(db: Database, chat_id: str) -> str | None:
    key = _pending_key(str(chat_id))
    val = db.memory_get(JOB, key)
    if val:
        db.memory_delete(JOB, key)
    return val


def _edit_prompt(data: str) -> str:
    """What the force-reply asks for. Plain, specific, one line each."""
    prefix, target, verdict = parse_data(data)
    card = "{}:{}".format(prefix, target)
    if verdict == "redo":
        return ("Reply with what should change for {}, e.g. shorter, mention "
                "their API workshop, drop the second paragraph.".format(esc(card)))
    return ("Reply with the corrected email for {}. A first line starting with "
            "Subject: replaces the subject.".format(esc(card)))


# ---------------------------------------------------------------------------
# update consumption
# ---------------------------------------------------------------------------

def _chat_of(update: dict) -> str:
    if "callback_query" in update:
        message = (update["callback_query"] or {}).get("message") or {}
    else:
        message = update.get("message") or {}
    return str((message.get("chat") or {}).get("id", "") or "")


def _allowed(cfg, chat_id: str) -> bool:
    """Only the configured chat may drive the bot. An unset TELEGRAM_CHAT_ID
    (console mode, tests) allows everything; a set one is an allowlist of one."""
    wanted = str(getattr(cfg, "telegram_chat_id", "") or "").strip()
    return not wanted or wanted == str(chat_id).strip()


def _chunks(text: str, size: int = CHUNK) -> list[str]:
    out: list[str] = []
    text = text or ""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def _run_command(cfg, db: Database, notifier: Notifier, text: str,
                 chat_id: str) -> None:
    head, _, args = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()          # "/run@sponsy_bot 3" -> "run"
    fn = COMMANDS.get(name)
    if fn is None:
        notifier.send("unknown command; /help")
        return
    ctx = Context(cfg=cfg, db=db, notifier=notifier, prefix="cmd", target=name,
                  verdict="command", text=args.strip(), chat_id=chat_id)
    try:
        result = fn(ctx, args.strip())
    except Exception as exc:                        # one bad command, one reply
        print("[approvals] /{} failed: {}".format(name, exc))
        result = "/{} failed: {}: {}".format(name, type(exc).__name__, exc)
    for chunk in _chunks(str(result or "(no output)")):
        notifier.send(esc(chunk))


def _handle_update(cfg, db: Database, notifier: Notifier, update: dict) -> bool:
    """Route one Telegram update. Returns True when it applied a verdict, a
    command or a pending edit; False when it was ignored or only nudged."""
    chat_id = _chat_of(update)
    query = update.get("callback_query")

    # (1) security: the bot answers exactly one chat
    if not _allowed(cfg, chat_id):
        print("[approvals] ignored update from chat {}".format(chat_id or "?"))
        if query:
            notifier.answer_callback(query.get("id", ""), "not your bot")
        return False

    if query:
        data = (query.get("data") or "").strip()
        callback_id = query.get("id", "")
        parsed = parse_data(data)
        if parsed is None:
            notifier.answer_callback(callback_id, "unrecognised action")
            return False
        # (2) edit / redo: park it and ask for the text
        if parsed[2] in NEEDS_TEXT:
            set_pending_edit(db, chat_id, data)
            notifier.answer_callback(callback_id, "send me the corrected email"
                                     if parsed[2] == "edit" else
                                     "send me the instruction")
            notifier.force_reply(_edit_prompt(data))
            return True
        # (3) everything else is a verdict
        toast = _dispatch(cfg, db, notifier, data, callback_id=callback_id,
                          chat_id=chat_id)
        notifier.answer_callback(callback_id, (toast or "")[:TOAST_LEN])
        return True

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        return False                                # stickers, photos, joins

    # (4) slash-commands
    if text.startswith("/"):
        _run_command(cfg, db, notifier, text, chat_id)
        return True

    # (5) the text a force-reply asked for
    pending = pop_pending_edit(db, chat_id)
    if pending:
        toast = _dispatch(cfg, db, notifier, pending, text=text, chat_id=chat_id,
                          use_edit=True)
        notifier.send(esc(toast or "done"))
        return True

    # (6) nothing was expected from you
    notifier.send(HELP_HINT)
    return False


def _offset(db: Database) -> int | None:
    raw = db.memory_get(JOB, "update_offset")
    return int(raw) + 1 if raw else None


def drain(cfg, db: Database, notifier: Notifier, limit: int = 100) -> int:
    """Apply whatever was tapped since the last pass. Non-blocking, and only
    meaningful for the real bot: the console notifier has no inbox."""
    if not isinstance(notifier, TelegramNotifier):
        return 0
    try:
        updates = notifier.get_updates(offset=_offset(db), timeout=0)
    except Exception as exc:
        print("[approvals] getUpdates failed: {}".format(exc))
        return 0
    handled = 0
    for update in updates[:limit]:
        # Persist the offset before handling so a poison update is skipped on
        # the next pass instead of being retried forever.
        db.memory_set(JOB, "update_offset", str(update["update_id"]))
        try:
            handled += int(_handle_update(cfg, db, notifier, update))
        except Exception as exc:
            print("[approvals] update {} failed: {}".format(
                update.get("update_id"), exc))
    return handled


def serve(cfg, db: Database, notifier: Notifier, poll: int = 25) -> None:
    """Long-poll until ctrl-c. One bad update never kills the loop."""
    if not isinstance(notifier, TelegramNotifier):
        raise SystemExit("serve needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; "
                         "without them use: python run.py verdict d:ID:send")
    who = ""
    try:
        who = " as @{}".format(notifier.get_me().get("username", "bot"))
    except Exception as exc:
        print("[approvals] getMe failed: {}".format(exc))
    print("[approvals] listening{} for chat {} ({} commands; ctrl-c to stop)".format(
        who, notifier.chat_id, len(COMMANDS)))
    try:
        while True:
            try:
                updates = notifier.get_updates(offset=_offset(db), timeout=poll)
            except Exception as exc:
                print("[approvals] {}".format(exc))
                time.sleep(5)
                continue
            for update in updates:
                db.memory_set(JOB, "update_offset", str(update["update_id"]))
                try:
                    _handle_update(cfg, db, notifier, update)
                except Exception as exc:
                    print("[approvals] update {} failed: {}".format(
                        update.get("update_id"), exc))
    except KeyboardInterrupt:
        print("\n[approvals] stopped")
