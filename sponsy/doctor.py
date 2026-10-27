"""Live checks of every integration and secret.

    python run.py doctor

The checks run in the order a first-time setup fails: files, database, model,
search, Telegram, mail, replies, then deliverability -- whether receivers
can trust the sending domain (SPF, DKIM, DMARC), the pace mail goes out at,
the unsubscribe path, the email itself -- and a reminder of the send gate.
Each one is wrapped so a crash inside an integration becomes a FAIL line with
the message, never a traceback -- the whole point of `doctor` is to be the
command that still runs when something is broken.

Sibling modules (llm, research, telegram, mail) are imported lazily inside the
check that needs them, so this file imports even when one of them is missing
or half-broken. Nothing here ever sends a message or an email: Telegram is
probed with getMe/getChat/getUpdates (no offset, so pending taps are left for
`drain`), mail with a login or a profile read, IMAP read-only.

Secrets are never printed. Key counts, usernames and addresses are.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, Config, Event, Voice

# Values the example files ship with. Any of these still present in event.yml
# means the drafter would assert them as fact in an email to a stranger.
PLACEHOLDERS = ("TODO", "Your Name", "example.org", "00000")

TELEGRAM_API = "https://api.telegram.org/bot{}/{}"
PING_QUERY = "hackathon sponsors"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # optional = a WARN: reported, gets a fix line, does not fail the exit code.
    optional: bool = False

    @property
    def status(self) -> str:
        if self.ok:
            return "OK"
        return "WARN" if self.optional else "FAIL"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _short(exc: BaseException, limit: int = 200) -> str:
    text = "{}: {}".format(type(exc).__name__, exc).replace("\n", " ").strip()
    return text[:limit]


def _pick(data: Any, *keys: str) -> str:
    """First non-empty key of a Bot API result dict; tolerates non-dicts so a
    sibling that returns a bare username still reads."""
    if isinstance(data, dict):
        for key in keys:
            val = data.get(key)
            if val:
                return str(val)
        return ""
    return str(data or "")


def _guard(name: str, fn: Callable[[], Check], optional: bool = False) -> Check:
    """Run one check; a crash becomes a FAIL line, never a traceback."""
    try:
        check = fn()
    except Exception as exc:                       # noqa: BLE001  -- by design
        return Check(name, False, _short(exc),
                     hint="this check crashed; fix the cause on the line above "
                          "or re-run with the integration disabled", optional=optional)
    check.name = name                              # names are the report's key
    return check


def _is_placeholder(value: str) -> bool:
    for marker in PLACEHOLDERS:
        if marker.isdigit():
            # The example phone's "00000", not the tail of a real "100000+".
            if re.search(r"(?<!\d)" + marker, value):
                return True
        elif marker in value:
            return True
    return False


def _placeholders_in(event: Event) -> list[str]:
    """'field still says X' for every placeholder left in the loaded event,
    including inside list entries such as team_contacts[0].phone: the email
    template renders those verbatim too."""
    fields: list[tuple[str, str]] = []
    for key, val in vars(event).items():
        if isinstance(val, str):
            fields.append((key, val))
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, str):
                    fields.append(("{}[{}]".format(key, i), item))
                elif isinstance(item, dict):
                    fields.extend(("{}[{}].{}".format(key, i, k), v)
                                  for k, v in item.items() if isinstance(v, str))
    fields.extend(("tiers[{}].name".format(i), t.name) for i, t in enumerate(event.tiers))
    return ["{} still says {!r}".format(key, val.strip()[:60])
            for key, val in fields if _is_placeholder(val)]


def _chat_ids(updates: Any) -> list[str]:
    """Distinct chat ids seen in a getUpdates result, labelled when possible."""
    seen: dict[str, str] = {}
    for upd in updates or []:
        if not isinstance(upd, dict):
            continue
        chats = []
        for key in ("message", "edited_message", "channel_post", "my_chat_member",
                    "chat_member"):
            chats.append((upd.get(key) or {}).get("chat"))
        chats.append(((upd.get("callback_query") or {}).get("message") or {}).get("chat"))
        for chat in chats:
            if not isinstance(chat, dict) or chat.get("id") is None:
                continue
            cid = str(chat["id"])
            seen.setdefault(cid, _pick(chat, "title", "username", "first_name"))
    return ["{} ({})".format(cid, label) if label else cid for cid, label in seen.items()]


def _bot_api(token: str, method: str, **params: Any) -> Any:
    """Direct Bot API GET, used only for a read the notifier does not expose."""
    import requests  # noqa: PLC0415

    resp = requests.get(TELEGRAM_API.format(token, method), params=params, timeout=20)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "HTTP {}".format(resp.status_code))
    return data.get("result")


def _smtp_login(cfg: Config) -> None:
    """STARTTLS + LOGIN + QUIT. Sends nothing; a bad app password raises here."""
    if int(cfg.smtp_port) == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, int(cfg.smtp_port), timeout=20)
    else:
        server = smtplib.SMTP(cfg.smtp_host, int(cfg.smtp_port), timeout=20)
        server.ehlo()
        server.starttls()
    try:
        server.login(cfg.smtp_user, cfg.smtp_password)
    finally:
        try:
            server.quit()
        except Exception:                          # noqa: BLE001
            pass


def _imap_login(cfg: Config) -> int:
    """Login, select the folder read-only, logout. Returns the message count."""
    conn = imaplib.IMAP4_SSL(cfg.imap_host, timeout=20)
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
        status, data = conn.select(cfg.imap_folder or "INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("select {}: {}".format(cfg.imap_folder, data))
        try:
            return int((data[0] or b"0").decode() if isinstance(data[0], bytes)
                       else data[0] or 0)
        except (ValueError, IndexError, TypeError):
            return 0
    finally:
        try:
            conn.logout()
        except Exception:                          # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def check_env(cfg: Config, root: Path | None = None) -> Check:
    path = Path(root or ROOT) / ".env"
    if path.exists():
        return Check(".env", True, str(path))
    # CI passes secrets as environment variables and has no .env at all; that
    # is a WARN, not a FAIL, when at least one secret visibly came from there.
    from_env = any((cfg.api_keys, cfg.openai_api_key, cfg.anthropic_api_key,
                    cfg.telegram_bot_token, cfg.smtp_password, cfg.imap_password))
    hint = "copy .env.example to {} and fill it in".format(path)
    if from_env:
        return Check(".env", False, "no {}; secrets are coming from the environment"
                     .format(path), hint, optional=True)
    return Check(".env", False, "{} is missing".format(path), hint)


def check_event(cfg: Config, event: Event | None = None) -> Check:
    """The file the next run reads, not just an object in memory: cli falls
    back to event.example.yml when event.yml is missing, and that example is
    full of placeholders the drafter would state as fact."""
    path = Path(cfg.event_path)
    hint_file = "copy data/event.example.yml to {} and edit it".format(path)
    if not path.exists():
        return Check("event.yml", False, "{} is missing".format(path), hint_file)
    if event is None:
        event = Event.load(path)                   # a parse error is the detail
    problems: list[str] = []
    if not event.tiers:
        problems.append("no tiers (add at least one under `tiers:`)")
    if not (event.sender_name or "").strip():
        problems.append("sender_name is empty")
    if not (event.reply_to or "").strip():
        problems.append("reply_to is empty")
    elif "@" not in event.reply_to:
        problems.append("reply_to {!r} is not an address".format(event.reply_to))
    problems.extend(_placeholders_in(event))
    detail = "{} | tiers: {}".format(event.event_name or "(unnamed event)",
                                     ", ".join(event.tier_names) or "none")
    if problems:
        return Check("event.yml", False, detail,
                     "edit {}: {}".format(path, "; ".join(problems)))
    return Check("event.yml", True, detail)


def check_voice(cfg: Config, voice: Voice | None = None,
                event: Event | None = None) -> Check:
    path = Path(cfg.voice_path)
    if voice is None:
        voice = Voice.load(path, event)
    detail = "{} | sender {} | {} sample email(s), {} tone note(s)".format(
        path if path.exists() else "no voice.yml at {}".format(path),
        voice.sender_name or "(unnamed)", len(voice.sample_emails),
        len(voice.tone_notes))
    # The question this check answers is "will the drafts sound like me?", and
    # that depends on what is in the voice, not on where it came from. A
    # missing file matters only because it means no samples -- so it is
    # reported alongside them, not as a failure of its own.
    hints: list[str] = []
    if not voice.sample_emails:
        if not path.exists():
            hints.append("copy data/voice.example.yml to {} and make it true "
                         "for you".format(path))
        hints.append("paste two or three real emails you wrote into "
                     "`sample_emails` in {} -- the biggest lever for "
                     "human-sounding drafts".format(path.name))
    if hints:
        return Check("voice.yml", False, detail, "; ".join(hints), optional=True)
    return Check("voice.yml", True, detail)


def check_database(cfg: Config) -> Check:
    from . import store  # noqa: PLC0415
    from .state import Database  # noqa: PLC0415

    path = Path(cfg.db_path)
    db = Database(path)
    try:
        store.ensure_schema(db)
        prospects = db.one("SELECT count(*) AS n FROM prospects")["n"]
        pending = db.one("SELECT count(*) AS n FROM drafts WHERE verdict IS NULL "
                         "AND sent_at IS NULL")["n"]
    finally:
        db.close()
    return Check("database", True, "{} ({} prospect(s), {} draft(s) waiting)".format(
        path, prospects, pending))


def check_model(cfg: Config, live: bool = True) -> Check:
    provider = cfg.resolved_llm_provider()
    key_hint = ("set one of GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY in "
                ".env; LLM_PROVIDER=auto picks the first one present")
    if provider == "fake":
        if not live:
            return Check("model", False, "no key configured; offline mode "
                         "(FakeLLM, fixtures only)", key_hint, optional=True)
        return Check("model", False, "no model key configured (provider=fake)",
                     key_hint)

    model, cheap = cfg.resolved_models()
    parts = ["{}: {} (cheap: {})".format(provider, model, cheap)]
    if provider == "gemini":
        n = len(cfg.api_keys)
        parts.append("{} key{} rotated on daily caps".format(n, "" if n == 1 else "s"))
        if cfg.min_interval_s > 0:
            parts.append("calls paced {:g}s apart (LLM_MIN_INTERVAL; 0 once "
                         "billing is on)".format(cfg.min_interval_s))
    if not live:
        parts.append("not pinged (offline)")
        return Check("model", True, ", ".join(parts))

    from . import llm as llm_mod  # noqa: PLC0415

    try:
        text = llm_mod.build_llm(cfg).ping()
    except llm_mod.QuotaExhausted as exc:
        return Check("model", False,
                     "daily quota exhausted: {}".format(str(exc)[:160]),
                     "add another key to GEMINI_API_KEYS (rotated automatically) "
                     "or enable billing on the project; the cap is per model, so "
                     "a different LLM_MODEL also works today")
    except Exception as exc:                       # noqa: BLE001  -- reported
        return Check("model", False, "ping failed: " + _short(exc),
                     "check the key and LLM_MODEL={} (a 404 means the model name "
                     "is wrong for this provider); pip install anthropic for "
                     "LLM_PROVIDER=anthropic".format(model))
    if not (text or "").strip():
        return Check("model", False, ", ".join(parts) + "; empty reply to a ping",
                     "the model answered with no text; try another LLM_MODEL")
    parts.append("ping ok ({!r})".format(text.strip()[:20]))
    return Check("model", True, ", ".join(parts))


def check_search(cfg: Config, live: bool = True) -> Check:
    provider = cfg.resolved_search_provider()
    tavily_hint = ("get a free Tavily key at tavily.com and set TAVILY_API_KEY "
                   "(SEARCH_PROVIDER=auto picks it up)")
    if provider == "none":
        return Check("search", False, "SEARCH_PROVIDER=none: research runs only "
                     "on stored or frozen sources", tavily_hint, optional=True)
    if provider == "ddg":
        detail = "keyless DuckDuckGo is fragile"
        if live:
            try:
                from . import research  # noqa: PLC0415
                hits = research.search(cfg, PING_QUERY, k=1)
                detail += "; it answered ({} result)".format(len(hits or []))
            except Exception as exc:               # noqa: BLE001
                detail += "; it failed just now: " + _short(exc, 120)
        return Check("search", False, detail, tavily_hint, optional=True)

    key = cfg.tavily_api_key if provider == "tavily" else cfg.brave_api_key
    if not key:
        return Check("search", False, "SEARCH_PROVIDER={} but no key".format(provider),
                     "set {} in .env".format("TAVILY_API_KEY" if provider == "tavily"
                                             else "BRAVE_API_KEY"))
    if not live:
        return Check("search", True, "{}: key set, not tested offline".format(provider))

    from . import research  # noqa: PLC0415

    try:
        hits = research.search(cfg, PING_QUERY, k=1)
    except Exception as exc:                       # noqa: BLE001
        return Check("search", False, "{}: search failed: {}".format(
            provider, _short(exc)), "check the key; free tiers rate-limit -- "
            "wait a minute and re-run doctor")
    if not isinstance(hits, list):
        return Check("search", False, "{}: search returned {} instead of a list"
                     .format(provider, type(hits).__name__),
                     "sponsy.research.search must return a list of hits")
    return Check("search", True, "{}: {} result(s) for {!r}".format(
        provider, len(hits), PING_QUERY))


def check_telegram(cfg: Config, live: bool = True) -> Check:
    token, chat_id = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not token:
        return Check("telegram", False, "TELEGRAM_BOT_TOKEN not set",
                     "create a bot with @BotFather and put its token in .env as "
                     "TELEGRAM_BOT_TOKEN (until then, verdicts come from "
                     "`python run.py verdict d:ID:send` in the terminal)")
    if not live:
        if not chat_id:
            return Check("telegram", False, "token set; TELEGRAM_CHAT_ID not set",
                         "message the bot once, then run `python run.py doctor` "
                         "online: it reads the chat id for you")
        return Check("telegram", True, "token and chat id set; not tested offline")

    from . import telegram as tg  # noqa: PLC0415

    bot = tg.TelegramNotifier(token, chat_id)
    try:
        me = bot.get_me()
    except Exception as exc:                       # noqa: BLE001
        return Check("telegram", False, "getMe failed: " + _short(exc),
                     "the token is wrong or revoked; paste it again from "
                     "@BotFather with no quotes or trailing space")
    username = _pick(me, "username", "first_name")
    parts = ["bot @{}".format(username) if username else "bot ok"]

    if not chat_id:
        # No offset on purpose: the updates stay unconfirmed for `drain`.
        ids: list[str] = []
        try:
            ids = _chat_ids(bot.get_updates(timeout=0))
        except Exception as exc:                   # noqa: BLE001
            parts.append("getUpdates failed: " + _short(exc, 100))
        if ids:
            hint = "message the bot once, then set TELEGRAM_CHAT_ID={}".format(
                ids[0].split(" ")[0] if len(ids) == 1
                else "<one of: {}>".format(", ".join(ids)))
        else:
            hint = ("message the bot once (open @{} and send /start), then run "
                    "doctor again: it prints the chat id to put in "
                    "TELEGRAM_CHAT_ID".format(username or "your bot"))
        parts.append("TELEGRAM_CHAT_ID not set")
        return Check("telegram", False, "; ".join(parts), hint)

    get_chat = getattr(bot, "get_chat", None)
    try:
        chat = get_chat() if callable(get_chat) else _bot_api(token, "getChat",
                                                              chat_id=chat_id)
    except Exception as exc:                       # noqa: BLE001
        return Check("telegram", False, "; ".join(parts) + "; getChat {} failed: {}"
                     .format(chat_id, _short(exc, 120)),
                     "the bot cannot see chat {}: message it once from that "
                     "account (or add it to the group), and check TELEGRAM_CHAT_ID"
                     .format(chat_id))
    title = _pick(chat, "title", "first_name", "username")
    parts.append("chat {}{}".format(chat_id, " ({})".format(title) if title else ""))
    return Check("telegram", True, "; ".join(parts))


def check_brochure(cfg: Config) -> Check:
    """The PDF that goes out with the first email. Optional: without one the
    emails are plain text, which is a perfectly good way to run."""
    from .mail.message import ATTACH_KINDS, attachment_problem  # noqa: PLC0415

    path = Path(cfg.brochure)
    if not path.exists():
        return Check("brochure", False,
                     "no brochure at {}; emails go out as plain text".format(path),
                     "drop your sponsorship PDF there (or point BROCHURE at it) "
                     "and it is attached automatically", optional=True)
    problem = attachment_problem(path)
    if problem:
        return Check("brochure", False, problem,
                     "fix or replace {}; until then nothing is attached".format(path),
                     optional=True)
    size = path.stat().st_size
    kinds = list(ATTACH_KINDS)
    named = (" and ".join(kinds) if len(kinds) < 3
             else "{} and {}".format(", ".join(kinds[:-1]), kinds[-1]))
    return Check("brochure", True, "{} ({:.1f} MB), attached to the {} email(s)"
                 .format(path.name, size / 1048576, named))


def check_mail(cfg: Config, live: bool = True) -> Check:
    provider = cfg.resolved_mail_provider()
    if provider == "manual":
        if not cfg.allow_send:
            detail = ("ALLOW_SEND=0 forces manual delivery: nothing will be sent; "
                      "[Send] writes an .eml to {} and gives a mailto link"
                      .format(cfg.outbox))
            hint = "set ALLOW_SEND=1 when you are ready to send for real"
        else:
            detail = ("manual: nothing will be sent; [Send] writes an .eml to {} "
                      "and gives a mailto link".format(cfg.outbox))
            hint = ("for real delivery use MAIL_PROVIDER=gmail_api (python run.py "
                    "gmail-auth) or smtp (SMTP_USER + a Gmail app password)")
        return Check("mail", False, detail, hint, optional=True)

    if provider == "gmail_api":
        creds, token = Path(cfg.gmail_credentials), Path(cfg.gmail_token)
        auth_hint = ("Google Cloud Console > APIs & Services > enable Gmail API > "
                     "Credentials > OAuth client ID (Desktop) > save the JSON to {} "
                     "then run: python run.py gmail-auth".format(creds))
        if not creds.exists() and not token.exists():
            return Check("mail", False, "gmail_api: neither {} nor {} exists".format(
                creds, token), auth_hint)
        if not live:
            return Check("mail", True, "gmail_api: {} present, not tested offline"
                         .format("token" if token.exists() else "credentials (not "
                                 "yet authorised: python run.py gmail-auth)"))
        try:
            from .mail.gmail_api import GmailClient  # noqa: PLC0415
        except ImportError as exc:
            return Check("mail", False, "gmail_api: " + _short(exc),
                         "pip install google-api-python-client google-auth-oauthlib "
                         "google-auth-httplib2")
        try:
            client = GmailClient(cfg)
            client.authorize(interactive=False)
            address = (client.profile() or {}).get("emailAddress", "")
        except Exception as exc:                   # noqa: BLE001
            return Check("mail", False, "gmail_api: " + _short(exc),
                         "run `python run.py gmail-auth` once (a browser opens); "
                         "if it keeps failing, delete {} and re-run it".format(token)
                         if creds.exists() else auth_hint)
        detail = "gmail_api: authorised as {}".format(address or "(unknown address)")
        if cfg.gmail_sender:
            detail += ", sending as {}".format(cfg.gmail_sender)
        return Check("mail", True, detail + " (try: python run.py test-mail)")

    # smtp
    missing = [name for name, val in (("SMTP_HOST", cfg.smtp_host),
                                      ("SMTP_USER", cfg.smtp_user),
                                      ("SMTP_PASSWORD", cfg.smtp_password)) if not val]
    if missing:
        return Check("mail", False, "smtp: {} not set".format(", ".join(missing)),
                     "SMTP_USER is your Gmail address; SMTP_PASSWORD is an App "
                     "Password (myaccount.google.com > Security > App passwords), "
                     "not the account password")
    where = "{} via {}:{}".format(cfg.smtp_user, cfg.smtp_host, cfg.smtp_port)
    if not live:
        return Check("mail", True, "smtp: {}, not tested offline".format(where))
    try:
        try:
            from .mail import smtp as smtp_mod  # noqa: PLC0415
            login = getattr(smtp_mod, "check_login", None)
        except ImportError:
            login = None
        if callable(login):
            login(cfg)
        else:
            _smtp_login(cfg)
    except Exception as exc:                       # noqa: BLE001
        return Check("mail", False, "smtp: login failed: " + _short(exc),
                     "use a Gmail App Password (myaccount.google.com > Security > "
                     "App passwords; needs 2-step verification), not the account "
                     "password; port 587 with STARTTLS")
    return Check("mail", True, "smtp: {}, login ok (try: python run.py test-mail)"
                 .format(where))


def _mailbox(address: str) -> str:
    """An address reduced to the inbox it lands in: lowercase, no +tag, and
    Gmail's ignored dots dropped, so R.Fanse+dotslash@gmail.com is
    rfanse@gmail.com."""
    local, _, domain = (address or "").strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return "{}@{}".format(local, domain) if local and domain else ""


def _reply_to_elsewhere(event: Event | None, *inboxes: str) -> str:
    """event.reply_to, when replies to it cannot land in any inbox we read.

    It is the Reply-To header of every email, so it is where sponsors'
    answers go. Empty when it matches, and when there is nothing to compare:
    no event, no reply_to, or an IMAP login that is not an address.
    """
    reply_to = (getattr(event, "reply_to", "") or "").strip()
    ours = {_mailbox(box) for box in inboxes if "@" in (box or "")} - {""}
    if not reply_to or not ours or _mailbox(reply_to) in ours:
        return ""
    return reply_to


def _unless_replies_go_elsewhere(check: Check, elsewhere: str, inbox: str) -> Check:
    """A passing replies check becomes a WARN when reply_to is another inbox:
    replies never arrive, so a "stop" goes unread and the ladder keeps going."""
    if not (check.ok and elsewhere):
        return check
    return Check(check.name, False,
                 "{}; but reply_to is {}, so replies go to an inbox this bot never "
                 "reads".format(check.detail, elsewhere),
                 "reply_to in event.yml becomes the Reply-To header of every email: "
                 "set it to {} (move SMTP_*, IMAP_* and reply_to together when you "
                 "switch addresses); ignore this only if {} forwards into {}".format(
                     inbox, elsewhere, inbox),
                 optional=True)


def _gmail_account(cfg: Config) -> str:
    """The authorised Gmail address, or "" -- check_mail reports why not."""
    try:
        from .mail.gmail_api import GmailClient  # noqa: PLC0415
        client = GmailClient(cfg)
        client.authorize(interactive=False)
        return (client.profile() or {}).get("emailAddress", "") or ""
    except Exception:                              # noqa: BLE001
        return ""


def check_replies(cfg: Config, live: bool = True, event: Event | None = None) -> Check:
    provider = cfg.resolved_replies_provider()
    if provider == "none":
        return Check("replies", False, "replies are not polled; feed them in with "
                     "`python run.py simulate-reply P01 --text ...`",
                     "use gmail_api mail (polling reuses that token) or set "
                     "IMAP_USER + IMAP_PASSWORD (a Gmail app password)", optional=True)
    if provider == "gmail_api":
        token = Path(cfg.gmail_token)
        if not token.exists():
            return Check("replies", False, "gmail_api: no token at {}".format(token),
                         "run `python run.py gmail-auth` once; the same token sends "
                         "and reads")
        check = Check("replies", True, "gmail_api: read-only via {}".format(token))
        # Replies are read from the authorised account, which is only known
        # after a network call; offline there is nothing reliable to compare.
        account = _gmail_account(cfg) if live and getattr(event, "reply_to", "") else ""
        if not account:
            return check
        return _unless_replies_go_elsewhere(
            check, _reply_to_elsewhere(event, account, cfg.gmail_sender), account)

    # imap
    missing = [name for name, val in (("IMAP_HOST", cfg.imap_host),
                                      ("IMAP_USER", cfg.imap_user),
                                      ("IMAP_PASSWORD", cfg.imap_password)) if not val]
    if missing:
        return Check("replies", False, "imap: {} not set".format(", ".join(missing)),
                     "set them in .env; for Gmail the password is an App Password "
                     "and IMAP must be enabled in Gmail settings")
    where = "{} at {}, folder {}".format(cfg.imap_user, cfg.imap_host,
                                         cfg.imap_folder or "INBOX")
    elsewhere = _reply_to_elsewhere(event, cfg.imap_user)
    if not live:
        return _unless_replies_go_elsewhere(
            Check("replies", True, "imap: {}, not tested offline".format(where)),
            elsewhere, cfg.imap_user)
    try:
        count = _imap_login(cfg)
    except Exception as exc:                       # noqa: BLE001
        return Check("replies", False, "imap: " + _short(exc),
                     "use a Gmail App Password, enable IMAP in Gmail settings "
                     "(Settings > Forwarding and POP/IMAP), and check IMAP_FOLDER")
    return _unless_replies_go_elsewhere(
        Check("replies", True, "imap: {}, read-only login ok ({} message(s))"
              .format(where, count)), elsewhere, cfg.imap_user)


# ---------------------------------------------------------------------------
# deliverability
# ---------------------------------------------------------------------------

DKIM_SELECTORS = ("google", "selector1", "selector2", "default", "dkim", "mail",
                  "k1", "s1", "s2", "smtp")


def _expected_spf(cfg: Config) -> str:
    """The include: a domain needs in its SPF to send through our provider."""
    provider = cfg.resolved_mail_provider()
    host = (cfg.smtp_host or "").lower()
    if provider == "gmail_api" or (provider == "smtp" and host.endswith("gmail.com")):
        return "_spf.google.com"
    return ""


def check_sender_domain(cfg: Config, event: Event | None = None, live: bool = True,
                        lookup=None) -> Check:
    """Can a receiver authenticate mail from our From address?

    Gmail requires SPF or DKIM of every sender and SPF + DKIM + DMARC of bulk
    senders; Outlook junks unauthenticated mail. A free @gmail.com address
    is Google's to authenticate, so there is nothing to check there.
    """
    from .mail import dns  # noqa: PLC0415
    from .mail.message import sender_address  # noqa: PLC0415

    if cfg.resolved_mail_provider() == "manual":
        return Check("sender domain", True, "manual delivery: your own mail client "
                     "sends, so its account's authentication applies", optional=True)
    sender = sender_address(cfg, event) if event is not None else (
        cfg.gmail_sender or cfg.smtp_from or cfg.smtp_user)
    domain = dns.domain_of(sender)
    if not domain:
        return Check("sender domain", False, "no From address to check",
                     "set GMAIL_SENDER (gmail_api) or SMTP_FROM / SMTP_USER (smtp)",
                     optional=True)
    notes = []
    login = (cfg.smtp_user or "").strip().lower()
    if cfg.resolved_mail_provider() == "smtp" and login and \
            dns.domain_of(login) != domain:
        notes.append("From is @{} but you log in as {}: Gmail rewrites the From "
                     "unless it is a verified 'Send mail as' alias".format(domain, login))
    if domain in dns.PROVIDER_DOMAINS:
        detail = ("{} is a {} address: SPF, DKIM and DMARC are {}'s, and they pass. "
                  "Fine at this volume; a club domain on Google Workspace gives you "
                  "your own reputation and DMARC reports".format(
                      sender, domain, domain.split(".")[0].title()))
        if notes:
            return Check("sender domain", False, detail + "; " + "; ".join(notes),
                         "send from the account you authenticate as", optional=True)
        return Check("sender domain", True, detail)
    if not live:
        return Check("sender domain", True, "{}: not checked offline".format(domain),
                     optional=True)
    selectors = [cfg.dkim_selector] + [s for s in DKIM_SELECTORS if s != cfg.dkim_selector]
    report = dns.check_sender_domain(domain, selectors, _expected_spf(cfg),
                                     lookup=lookup or dns.query)
    if report.errors and not (report.spf or report.dkim or report.dmarc):
        return Check("sender domain", False, "{}: DNS lookups failed ({})".format(
            domain, "; ".join(report.errors)[:160]),
            "check this machine's internet access and re-run doctor", optional=True)
    good = []
    if report.spf and not report.spf_problem:
        good.append("SPF ok")
    if report.dkim:
        good.append("DKIM '{}'{}".format(report.dkim, " ({}-bit)".format(
            report.dkim_bits) if report.dkim_bits else ""))
    if report.dmarc:
        good.append("DMARC p={}".format(report.dmarc_policy or "?"))
    problems = report.problems() + notes
    detail = "{}: {}".format(domain, ", ".join(good) or "nothing authenticates it")
    if problems:
        detail += " | " + "; ".join(problems)
    hint_bits = []
    if not report.spf or report.spf_problem:
        hint_bits.append('a TXT record on {} like "v=spf1 include:{} ~all"'.format(
            domain, _expected_spf(cfg) or "<your provider's SPF host>"))
    if not report.dkim:
        hint_bits.append("DKIM: Google Admin > Apps > Gmail > Authenticate email > "
                         "generate a 2048-bit key and publish it (selector 'google'); "
                         "set DKIM_SELECTOR if yours differs")
    if not report.dmarc:
        hint_bits.append('a TXT record on _dmarc.{} like "v=DMARC1; p=none; '
                         'rua=mailto:dmarc@{}" (start at p=none, tighten later)'.format(
                             domain, domain))
    if not report.mx:
        hint_bits.append("an MX record so replies and bounces reach you")
    hint = "add " + "; ".join(hint_bits) if hint_bits else "; ".join(notes)
    if not report.ok:
        return Check("sender domain", False, detail, hint + ". Gmail and Outlook junk "
                     "or reject mail with neither SPF nor DKIM")
    return Check("sender domain", not problems, detail, hint, optional=True)


def check_pacing(cfg: Config) -> Check:
    """The send-rate policy, and whether its settings parse."""
    from .mail import pacing  # noqa: PLC0415

    errors = []
    for fn, value in ((pacing.parse_tz, cfg.send_tz), (pacing.parse_hours, cfg.send_hours),
                      (pacing.parse_days, cfg.send_days)):
        try:
            fn(value)
        except ValueError as exc:
            errors.append(str(exc))
    policy = pacing.Policy.from_cfg(cfg)
    detail = policy.describe()
    if not cfg.paced:
        detail = "not used yet (mail is {}); when it is: {}".format(
            "a dry run" if cfg.dry_run else cfg.resolved_mail_provider(), detail)
    if errors:
        return Check("pacing", False, "; ".join(errors), "fix the value in .env; until "
                     "then the default is used", optional=True)
    warn = []
    if policy.daily_cap > 100:
        warn.append("SEND_DAILY_CAP={} is far more than a person sends; reputation "
                    "suffers long before Gmail's own 500/2,000 limit".format(
                        policy.daily_cap))
    if policy.min_gap_s < 30:
        warn.append("SEND_MIN_GAP={}s sends in bursts".format(policy.min_gap_s))
    if not policy.warmup:
        warn.append("SEND_WARMUP=0: a new sender going straight to full volume is "
                    "a classic spam pattern")
    if warn:
        return Check("pacing", False, detail + " | " + "; ".join(warn),
                     "the defaults in .env.example are a safe start", optional=True)
    return Check("pacing", True, detail)


def check_unsubscribe(cfg: Config) -> Check:
    url = (cfg.public_url or "").strip()
    if not url:
        return Check("unsubscribe", False,
                     "mailto unsubscribe header only; Gmail's one-click unsubscribe "
                     "needs an https endpoint",
                     "deploy `web` behind HTTPS and set PUBLIC_URL to its address "
                     "(see deploy/README.md); every email then carries a signed "
                     "one-click link", optional=True)
    if not url.lower().startswith("https://"):
        return Check("unsubscribe", False, "PUBLIC_URL={} is not https".format(url),
                     "RFC 8058 one-click needs https; put the web UI behind a TLS "
                     "proxy (deploy/Caddyfile)", optional=True)
    return Check("unsubscribe", True, "one-click via {}/u/... plus the mailto "
                 "header".format(url.rstrip("/")))


GMAIL_CLIP_BYTES = 102 * 1024


def check_content(cfg: Config, event: Event | None = None,
                  voice: Voice | None = None) -> Check:
    """The fixed parts of the email: what every recipient gets around the
    prose. Gmail clips HTML over ~102 KB, hiding the footer and the way out."""
    if event is None:
        return Check("email content", False, "no event.yml to render", optional=True)
    from types import SimpleNamespace  # noqa: PLC0415

    from .mail.message import build_message  # noqa: PLC0415
    from .mail.spamcheck import URL  # noqa: PLC0415

    sample = SimpleNamespace(id="DOCTOR", company="Example Co", contact_name="Sam",
                             email="sam@example.com")
    prose = "Hi Sam,\n\nA paragraph about why we are writing.\n\nThanks"
    msg = build_message(cfg, event, voice or SimpleNamespace(sender_name="",
                                                             signoff=""),
                        sample, "A short subject", prose, kind="initial")
    html_part = msg.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    size = len(html.encode("utf-8"))
    total = len(bytes(msg))
    links = sorted(set(URL.findall(html)))
    plain = [u for u in links if u.lower().startswith("http://")]
    problems = []
    if size > GMAIL_CLIP_BYTES:
        problems.append("the HTML is {:.0f} KB; Gmail clips over 102 KB, hiding the "
                        "footer and the opt-out line".format(size / 1024))
    if plain:
        problems.append("http:// links in the template: {}".format(", ".join(plain[:3])))
    if not (getattr(event, "opt_out", "") or "").strip():
        problems.append("opt_out is empty in event.yml, so the email has no visible "
                        "way out (recipients use 'Report spam' instead)")
    if "List-Unsubscribe" not in msg:
        problems.append("no List-Unsubscribe header (no From address?)")
    detail = "first email: {:.0f} KB HTML, {:.0f} KB with images and attachment, " \
             "{} distinct link(s)".format(size / 1024, total / 1024, len(links))
    if problems:
        return Check("email content", False, detail + " | " + "; ".join(problems),
                     "trim the event.yml blocks or the images; use https links",
                     optional=True)
    return Check("email content", True, detail)


def check_send_gate(cfg: Config) -> Check:
    """Not a check, a reminder: it is printed so the invariant is on screen."""
    parts = ["ALLOW_SEND is {}".format("on" if cfg.allow_send else "off")]
    if getattr(cfg, "dry_run", False):
        parts.append("--dry-run: nothing is sent this session")
    parts.append("nothing is sent without a tap on [Send] in Telegram (or "
                 "`python run.py verdict d:ID:send`); a database trigger refuses "
                 "a send with no recorded verdict")
    parts.append("every approved email is written to {} first".format(cfg.outbox))
    if cfg.paced:
        parts.append("approved emails leave through the outbox at the paced rate "
                     "(python run.py outbox)")
    if cfg.send_only_to:
        parts.append("SEND_ONLY_TO is set, so [Send] only reaches {} -- "
                     "every other address is held as an .eml (clear it "
                     "when testing is over)".format(", ".join(cfg.send_only_to)))
    return Check("send gate", True, "; ".join(parts), optional=True)


# ---------------------------------------------------------------------------
# run + report
# ---------------------------------------------------------------------------

def _quiet_load_event(cfg: Config) -> Event | None:
    """For the voice fallback only; the event check reports the real error."""
    try:
        return Event.load(cfg.event_path)
    except Exception:                              # noqa: BLE001
        return None


def run_checks(cfg: Config, event: Event | None = None, voice: Voice | None = None,
               live: bool = True) -> list[Check]:
    """Every check, in order, none of them raising.

    `event` and `voice` are the objects the caller already loaded (cli passes
    them to avoid parsing the files twice); pass None and the doctor reads the
    files itself, which is what a test that wants to inspect a file should do.
    `live=False` skips every network call and reports presence only.
    """
    checks: list[Check] = []
    checks.append(_guard(".env", lambda: check_env(cfg)))
    checks.append(_guard("event.yml", lambda: check_event(cfg, event)))
    if event is None and Path(cfg.event_path).exists():
        event = _quiet_load_event(cfg)
    checks.append(_guard("voice.yml", lambda: check_voice(cfg, voice, event),
                         optional=True))
    checks.append(_guard("database", lambda: check_database(cfg)))
    checks.append(_guard("model", lambda: check_model(cfg, live)))
    checks.append(_guard("search", lambda: check_search(cfg, live)))
    checks.append(_guard("telegram", lambda: check_telegram(cfg, live)))
    checks.append(_guard("mail", lambda: check_mail(cfg, live)))
    checks.append(_guard("brochure", lambda: check_brochure(cfg)))
    checks.append(_guard("replies", lambda: check_replies(cfg, live, event)))
    checks.append(_guard("sender domain",
                         lambda: check_sender_domain(cfg, event, live), optional=True))
    checks.append(_guard("pacing", lambda: check_pacing(cfg), optional=True))
    checks.append(_guard("unsubscribe", lambda: check_unsubscribe(cfg), optional=True))
    checks.append(_guard("email content", lambda: check_content(cfg, event, voice),
                         optional=True))
    checks.append(_guard("send gate", lambda: check_send_gate(cfg), optional=True))
    return checks


def format_report(checks: list[Check]) -> str:
    """The table, the fix lines, the verdict. Pure, so it can be tested."""
    rows = list(checks or [])
    width = max([len(str(c.name)) for c in rows] + [4])
    lines = []
    for check in rows:
        detail = str(check.detail or "").replace("\n", " / ")
        lines.append("{:<4}  {:<{w}}  {}".format(check.status, check.name, detail,
                                                 w=width).rstrip())
    fixes = [c for c in rows if not c.ok and (c.hint or "").strip()]
    if fixes:
        lines.append("")
        for check in fixes:
            lines.append("fix: {} - {}".format(check.name,
                                               str(check.hint).replace("\n", " ")))
    problems = sum(1 for c in rows if not c.ok and not c.optional)
    lines.append("")
    if problems:
        lines.append("{} problem(s) to fix before a live run".format(problems))
    else:
        lines.append("all good - run: python run.py run --limit 3")
    return "\n".join(lines)


def print_report(checks: list[Check]) -> None:
    """Never raises: a doctor that crashes while printing helps nobody."""
    try:
        print(format_report(checks))
    except Exception as exc:                       # noqa: BLE001
        print("doctor: could not format the report ({})".format(_short(exc)))
        for check in checks or []:
            try:
                print("{} {}: {}".format(getattr(check, "status", "?"),
                                         getattr(check, "name", "?"),
                                         getattr(check, "detail", "")))
            except Exception:                      # noqa: BLE001
                pass
