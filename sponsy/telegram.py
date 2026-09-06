"""Telegram is the entire frontend.

Two implementations of one small interface: the real Bot API, and a console
notifier used when no token is configured (offline demos, CI, tests). Every
outgoing text also lands in data/outbox/notifications.log, so an unattended
run leaves a readable trail even when Telegram is down or the phone is off.

The card lifecycle relies on `edit`: a draft card is posted with buttons, and
after a verdict the same message is rewritten ("Sent", "skipped", ...) with
the buttons removed, so a stale card can never be tapped twice.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

API = "https://api.telegram.org/bot{}/{}"
MAX_LEN = 4000                 # Telegram's limit is 4096; keep room for a footer
TOAST_LEN = 200                # answerCallbackQuery text limit

# One row per inner list; each button is (label, callback_data).
Buttons = list[list[tuple[str, str]]]

# Telegram descriptions that mean "the edit did not happen, and retrying will
# not help": the text is unchanged, or the message is gone / not ours.
_EDIT_NOOPS = ("not modified", "message to edit not found", "message not found",
               "can't be edited", "message_id_invalid", "message to delete")


def esc(text: Any) -> str:
    """Escape for parse_mode=HTML. Quotes stay readable in a draft body."""
    return html.escape(str(text), quote=False)


def _clip(text: str, limit: int = MAX_LEN) -> str:
    return text if len(text) <= limit else text[:limit - 20] + "\n... (truncated)"


def _keyboard(buttons: Buttons | None) -> dict | None:
    if not buttons:
        return None
    rows = [[{"text": label, "callback_data": data} for label, data in row]
            for row in buttons]
    return {"inline_keyboard": rows}


def _unhtml(text: str) -> str:
    """Plain text for the console and the log: tags off, entities decoded."""
    return html.unescape(re.sub(r"</?(b|i|pre|code|a|u|s|tg-spoiler)\b[^>]*>", "",
                                text or ""))


def _append_log(logfile: Path | None, block: str) -> None:
    if not logfile:
        return
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _print(block: str) -> None:
    # Button labels carry emoji; a cp1252 console must not turn a demo run
    # into a UnicodeEncodeError.
    try:
        print(block)
    except UnicodeEncodeError:
        print(block.encode("ascii", "replace").decode("ascii"))


class Notifier:
    """What the job and the approval loop are written against."""

    def send(self, text: str, buttons: Buttons | None = None,
             html_mode: bool = True) -> int | None:
        raise NotImplementedError

    def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> bool:
        raise NotImplementedError

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def force_reply(self, text: str) -> int | None:
        raise NotImplementedError

    # -- shared formatting -------------------------------------------------
    def info(self, job: str, summary: str) -> int | None:
        return self.send("[{}] {}".format(esc(job), esc(summary)))

    def escalate(self, job: str, result) -> int | None:
        body = ["<b>[{}]</b> needs you".format(esc(job)), esc(result.summary)]
        if getattr(result, "reason", ""):
            body.append("reason: " + esc(result.reason))
        body.append("confidence {:.2f}".format(getattr(result, "confidence", 0.0)))
        return self.send("\n".join(body))

    def error(self, job: str, exc: BaseException) -> int | None:
        return self.send("<b>[{}] FAILED</b>\n<pre>{}</pre>".format(
            esc(job), esc("{}: {}".format(type(exc).__name__, exc))[:1500]))

    def run_footer(self, run_id: int, steps: int, cost: float, elapsed: float) -> str:
        return "run #{} - {} steps - ${:.4f} - {:.0f}s".format(
            run_id, steps, cost, elapsed)


class ConsoleNotifier(Notifier):
    """Prints what Telegram would have shown. Buttons become hints, and the
    hint spells out the terminal command that does the same thing, so an
    offline demo teaches the real workflow."""

    def __init__(self, logfile: Path | None = None):
        self.logfile = Path(logfile) if logfile else None
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.toasts: list[str] = []
        self._id = 0

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _emit(self, block: str) -> None:
        _print(block)
        _append_log(self.logfile, block)

    def send(self, text: str, buttons: Buttons | None = None,
             html_mode: bool = True) -> int | None:
        message_id = self._next()
        plain = _unhtml(text)
        hint = ""
        if buttons:
            labels = [label for row in buttons for label, _ in row]
            data = [data for row in buttons for _, data in row]
            hint = ("\n  " + "  ".join("[{}]".format(x) for x in labels)
                    + "\n  from the terminal: python run.py verdict " + data[0])
        self._emit("\n" + "-" * 68 + "\n" + plain + hint + "\n")
        self.sent.append({"text": plain, "buttons": [list(row) for row in buttons or []]})
        return message_id

    def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> bool:
        plain = _unhtml(text)
        self._emit("\n" + "-" * 68 + "\n(edit #{})\n{}\n".format(message_id, plain))
        self.edited.append({"message_id": message_id, "text": plain,
                            "buttons": [list(row) for row in buttons or []]})
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.toasts.append(text)
        if text:
            self._emit("  toast: {}\n".format(text))

    def force_reply(self, text: str) -> int | None:
        message_id = self._next()
        plain = _unhtml(text)
        self._emit("\n" + "-" * 68 + "\n" + plain + "\n  (reply expected)\n")
        self.sent.append({"text": plain, "buttons": [], "force_reply": True})
        return message_id


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, logfile: Path | None = None):
        self.token, self.chat_id = token, str(chat_id)
        self.logfile = Path(logfile) if logfile else None

    # -- transport ---------------------------------------------------------
    def _post(self, method: str, payload: dict) -> Any:
        import requests  # noqa: PLC0415

        resp = requests.post(API.format(self.token, method), json=payload, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("telegram {}: HTTP {} {}".format(
                method, resp.status_code, resp.text[:200]))
        if not data.get("ok"):
            raise RuntimeError("telegram {}: {}".format(
                method, data.get("description") or data))
        return data.get("result")

    def _log(self, text: str) -> None:
        _append_log(self.logfile, "\n" + "-" * 68 + "\n" + _unhtml(text) + "\n")

    # -- messages ----------------------------------------------------------
    def send(self, text: str, buttons: Buttons | None = None,
             html_mode: bool = True) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": _clip(text),
            "disable_web_page_preview": True,
        }
        if html_mode:
            payload["parse_mode"] = "HTML"
        markup = _keyboard(buttons)
        if markup:
            payload["reply_markup"] = markup
        self._log(text)
        result = self._post("sendMessage", payload)
        return int(result["message_id"])

    def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> bool:
        """Rewrite a card in place. With buttons=None the keyboard is removed
        explicitly (omitting reply_markup would leave the old buttons live).
        Returns False, rather than raising, when nothing could change: same
        text, or the message is gone."""
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": int(message_id),
            "text": _clip(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _keyboard(buttons) or {"inline_keyboard": []},
        }
        self._log(text)
        try:
            self._post("editMessageText", payload)
        except RuntimeError as exc:
            low = str(exc).lower()
            if any(marker in low for marker in _EDIT_NOOPS):
                return False
            raise
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._post("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text[:TOAST_LEN]})
        except Exception:
            pass  # a failed toast must never break the verdict write

    def force_reply(self, text: str) -> int | None:
        self._log(text)
        result = self._post("sendMessage", {
            "chat_id": self.chat_id, "text": _clip(text), "parse_mode": "HTML",
            "reply_markup": {"force_reply": True, "selective": True,
                             "input_field_placeholder": "your text"},
        })
        return int(result["message_id"])

    # -- polling & diagnostics ---------------------------------------------
    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        import requests  # noqa: PLC0415

        payload: dict[str, Any] = {"timeout": int(timeout),
                                   "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = int(offset)
        # The HTTP timeout must outlive the long-poll, or every quiet poll
        # ends in a spurious ReadTimeout.
        resp = requests.post(API.format(self.token, "getUpdates"), json=payload,
                             timeout=int(timeout) + 20)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError("telegram getUpdates: {}".format(
                data.get("description") or data))
        return list(data.get("result") or [])

    def get_me(self) -> dict:
        return self._post("getMe", {}) or {}

    def get_chat(self) -> dict:
        return self._post("getChat", {"chat_id": self.chat_id}) or {}


def build_notifier(cfg) -> Notifier:
    logfile = Path(cfg.outbox) / "notifications.log"
    # `--offline` / `demo` means no network, and Telegram is network. A demo
    # run on frozen fixtures must not push a real message to a real phone just
    # because a token happens to be sitting in .env.
    if cfg.telegram_enabled and not getattr(cfg, "offline", False):
        return TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, logfile)
    return ConsoleNotifier(logfile)
