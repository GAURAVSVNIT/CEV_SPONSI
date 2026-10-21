"""Browser frontend: the approval queue, the prospect list and the agent's
controls, on localhost.

    python run.py web                 http://127.0.0.1:8765
    python run.py web --demo          offline, on data/demo.db, frozen sources

It is one more way to tap a button, not a second set of rules. Every verdict
goes through approvals.apply_verdict -- the call the terminal and Telegram
make -- so the triggers in store.py gate it exactly as they gate those, and a
card already posted to Telegram is rewritten in place when you decide here.

Standard library only: the page is a few static files in sponsy/static and
the API is JSON over http.server. It binds to 127.0.0.1. Anything wider needs
a token, because this page can send email.

SQLite connections do not cross threads, so every request opens its own (WAL
makes that cheap). Verdicts and agent tasks take `busy`, one at a time: the
approval handlers are module-level and a [Redo] calls back into the job that
registered them. A request that finds it held gets a 409 rather than queueing
behind a ten-minute run.

Two things here are public on purpose, because they have to be:

  /u/<id>/<sig>   the one-click unsubscribe link in every email (RFC 8058).
                  A mail provider POSTs to it with no cookie and no token, so
                  it is authenticated by its HMAC signature instead, and all
                  it can ever do is stop email to one prospect.
  /healthz        for Docker and uptime checks; says "ok" and nothing else.

A background thread sends the outbox every few seconds while the page runs,
so a server with only `web` (no Telegram) still delivers approved email.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import sqlite3
import sys
import tempfile
import threading
import traceback
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import approvals, approve, outbox, store, unsubscribe
from .config import DATA
from .llm import LLMError, build_llm
from .models import STATUSES
from .state import Database, utcnow
from .telegram import build_notifier

STATIC = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}
# No inline script, no third-party anything: the page talks to this server only.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
MAX_BODY = 256 * 1024
LOG_CHARS = 20000
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
UNSUB_RE = re.compile(r"/u/([A-Za-z0-9_-]{1,40})/([0-9a-f]{32})/?")
# The unsubscribe page is a plain form with no script at all.
PUBLIC_CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
              "frame-ancestors 'none'; base-uri 'none'")
DISPATCH_EVERY = 15                     # seconds between outbox passes

VERDICTS = ("send", "edit", "redo", "skip", "never")
# name -> default limit
TASKS = {"run": None, "discover": 15, "find-contacts": 10, "doctor": 0}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Raw:
    """A non-JSON response (the CSV export)."""
    body: bytes
    content_type: str
    filename: str = ""


@dataclass
class Task:
    id: int
    name: str
    state: str = "running"             # running | done | failed
    started_at: str = ""
    finished_at: str = ""
    result: Any = None
    error: str = ""
    log: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "result": self.result, "error": self.error,
                "log": "".join(self.log)[-LOG_CHARS:]}


class _Tee:
    """sys.stdout that also copies a task thread's prints into its Task, so the
    page shows the same progress lines the terminal does."""

    def __init__(self, stream):
        self.stream = stream
        self.sinks: dict[int, list] = {}

    def write(self, text: str) -> int:
        sink = self.sinks.get(threading.get_ident())
        if sink is not None:
            sink.append(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


# ---------------------------------------------------------------------------
# JSON shapes
# ---------------------------------------------------------------------------

def _loads(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def draft_json(row) -> dict:
    """A drafts row (joined with its prospect) as the card shows it: the same
    numbers and the same failed-check list approve.draft_message prints."""
    data = dict(row)
    detail = approve._detail_from_row(row)
    checks = detail.get("checks") or {}
    soft = detail.get("soft_failures") or []
    claims = detail.get("claims") or _loads(data.get("claims"), [])
    ungrounded = detail.get("ungrounded") or []
    data.pop("checks", None)
    data.update({
        "verified": bool(data.get("verified")),
        "humanized": bool(data.get("humanized")),
        "words": detail.get("words") or len((data.get("body") or "").split()),
        "sources": detail.get("sources") or 0,
        "claims": [c if isinstance(c, dict) else {"claim": str(c)} for c in claims],
        "grounded": approve._grounded_count(claims, ungrounded),
        "ungrounded": [str(c) for c in ungrounded],
        "failed": [name for name, ok in checks.items() if not ok and name not in soft],
        "soft_failed": [str(name) for name in soft],
        "lint": [str(i) for i in ((detail.get("lint") or {}).get("issues") or [])][:5],
        "attachment": detail.get("attachment") or "",
        "spam_risks": approve.spam_risks(row),
    })
    return data


def _html(text) -> str:
    import html  # noqa: PLC0415
    return html.escape(str(text or ""), quote=True)


def _int(value, default: int, low: int = 1, high: int = 50) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _clean_email(value) -> str:
    email = (value or "").strip().lower()
    if email and not EMAIL_RE.fullmatch(email):
        raise ApiError(400, "that does not look like an email address: " + email)
    return email


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------

class WebApp:
    def __init__(self, cfg, event, voice, limit: int = 5,
                 frozen: dict | None = None, token: str = ""):
        self.cfg = cfg
        self.event = event
        self.voice = voice
        self.limit = limit
        self.frozen = frozen or {}
        self.token = token or ""
        self.loopback = True
        self.busy = threading.Lock()
        self.task: Task | None = None
        self.tee: _Tee | None = None
        self._task_ids = 0
        db = Database(cfg.db_path)
        try:
            store.ensure_schema(db)
        finally:
            db.close()
        self.routes = [
            ("GET", r"/api/overview", self.overview),
            ("GET", r"/api/drafts", self.drafts),
            ("POST", r"/api/drafts/(\d+)/(\w+)", self.verdict),
            ("GET", r"/api/prospects", self.prospects),
            ("POST", r"/api/prospects", self.add),
            ("POST", r"/api/prospects/bulk", self.add_many),
            ("GET", r"/api/prospects/([^/]+)", self.prospect),
            ("POST", r"/api/prospects/([^/]+)/email", self.set_email),
            ("POST", r"/api/prospects/([^/]+)/never", self.never),
            ("GET", r"/api/log", self.log),
            ("GET", r"/api/export.csv", self.export),
            ("GET", r"/api/task", self.task_status),
            ("POST", r"/api/tasks/([\w-]+)", self.start_task),
            ("GET", r"/api/outbox", self.outbox_status),
            ("POST", r"/api/outbox/pause", self.outbox_pause),
            ("POST", r"/api/outbox/resume", self.outbox_resume),
            ("POST", r"/api/outbox/send", self.outbox_send),
            ("POST", r"/api/outbox/(\d+)/cancel", self.outbox_cancel),
        ]
        self.stop = threading.Event()

    # -- plumbing ----------------------------------------------------------
    def handle(self, method: str, path: str, body: dict | None = None):
        """(status, payload). Never raises: a bad request is a JSON error."""
        for verb, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if verb == method and match:
                break
        else:
            return 404, {"error": "no such endpoint: {} {}".format(method, path)}
        db = Database(self.cfg.db_path)
        try:
            return 200, fn(db, body or {}, *match.groups())
        except ApiError as exc:
            return exc.status, {"error": str(exc)}
        except LLMError as exc:                     # QuotaExhausted is one too
            return 503, {"error": "stopped: {}".format(exc)}
        except Exception as exc:                    # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": "{}: {}".format(type(exc).__name__, exc)}
        finally:
            db.close()

    @contextmanager
    def _exclusive(self):
        if not self.busy.acquire(blocking=False):
            raise ApiError(409, self._busy_message())
        try:
            yield
        finally:
            self.busy.release()

    def _busy_message(self) -> str:
        if self.task and self.task.state == "running":
            return "the agent is busy ({}); try again when it finishes".format(
                self.task.name)
        return "another action is in progress; try again in a moment"

    def _job(self, db, notifier, limit: int | None = None):
        """A job registers the approval handlers ([Redo] calls back into it)."""
        from .job import SponsorshipJob  # noqa: PLC0415
        return SponsorshipJob(self.cfg, db, notifier, llm=build_llm(self.cfg, db=db),
                              today=None, force=True, event=self.event,
                              voice=self.voice, limit=limit or self.limit,
                              frozen=self.frozen)

    def authorized(self, supplied: str) -> bool:
        return not self.token or hmac.compare_digest(
            (supplied or "").encode(), self.token.encode())

    def host_ok(self, host_header: str) -> bool:
        """On loopback, only loopback names -- and PUBLIC_URL's host, for a
        reverse proxy in front -- so a DNS-rebound page cannot reach a
        server that can send email."""
        if not self.loopback:
            return True
        host = (host_header or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0]
        return host in LOOPBACK_NAMES or self.via_public(host_header)

    def via_public(self, host_header: str) -> bool:
        """Is this request addressed to PUBLIC_URL's host name?"""
        public = (urlsplit(getattr(self.cfg, "public_url", "") or "").hostname or "").lower()
        host = (host_header or "").strip().lower()
        host = host[1:].split("]", 1)[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        return bool(public) and host == public

    # -- public: unsubscribe and health ------------------------------------
    def unsubscribe(self, method: str, pid: str, sig: str, form: str) -> tuple:
        """(status, html bytes, content type). GET shows a confirm button and
        changes nothing -- link scanners and previews fetch every URL in an
        email. POST does it: the provider's one-click
        `List-Unsubscribe=One-Click`, or the button on the page."""
        if not unsubscribe.verify(self.cfg, pid, sig):
            return 404, unsubscribe.page("Link not recognised",
                                         "<p>This unsubscribe link is not valid. "
                                         "Reply to the email instead and we will "
                                         "stop writing.</p>"), "text/html; charset=utf-8"
        name = getattr(self.event, "club", "") or getattr(self.event, "event_name", "")
        if method == "GET":
            return 200, unsubscribe.page(
                "Stop these emails?",
                "<p>You will not hear from {} about sponsorship again.</p>"
                '<form method="post"><input type="hidden" name="confirm" value="1">'
                "<button type=submit>Yes, stop emailing me</button></form>".format(
                    _html(name or "us"))), "text/html; charset=utf-8"
        one_click = "list-unsubscribe=one-click" in form.lower()
        if not one_click and "confirm=1" not in form:
            return 400, b"bad request", "text/plain; charset=utf-8"
        db = Database(self.cfg.db_path)
        try:
            unsubscribe.apply(db, build_notifier(self.cfg), pid,
                              how="one-click header" if one_click else "unsubscribe link")
        finally:
            db.close()
        if one_click:
            return 200, b"unsubscribed", "text/plain; charset=utf-8"
        return 200, unsubscribe.page("Done", "<p>You are unsubscribed. Sorry for the "
                                     "bother.</p>"), "text/html; charset=utf-8"

    def health(self) -> tuple[int, dict]:
        try:
            db = Database(self.cfg.db_path)
            try:
                db.one("SELECT 1")
            finally:
                db.close()
        except Exception:                           # noqa: BLE001
            return 503, {"ok": False}
        return 200, {"ok": True}

    # -- the outbox, sent from a background thread ---------------------------
    def dispatch_loop(self, every: float = DISPATCH_EVERY) -> None:
        while not self.stop.wait(every):
            if self.cfg.offline:
                continue
            db = Database(self.cfg.db_path)
            try:
                outbox.dispatch(self.cfg, db, build_notifier(self.cfg), self.event,
                                self.voice)
            except Exception as exc:                # noqa: BLE001
                print("[web] outbox: {}".format(exc))
            finally:
                db.close()

    # -- reads -------------------------------------------------------------
    def overview(self, db, body) -> dict:
        cfg, event = self.cfg, self.event
        stats = store.stats(db)
        upcoming = db.query(
            "SELECT id, company, status, touches, next_touch FROM prospects "
            "WHERE next_touch IS NOT NULL AND status NOT IN ('declined','cold') "
            "ORDER BY next_touch LIMIT 12")
        runs = db.query("SELECT id, status, steps, cost_usd, summary, error, "
                        "started_at, finished_at FROM runs ORDER BY id DESC LIMIT 5")
        return {
            "event": {"name": event.event_name, "date": event.date,
                      "club": event.club, "where": event.where()},
            "stats": dict(stats, awaiting_contact=len(stats["awaiting_contact"]),
                          pending=stats["drafts"].get("pending", 0)),
            "channels": {"llm": cfg.resolved_llm_provider(),
                         "search": cfg.resolved_search_provider(),
                         "mail": cfg.resolved_mail_provider(),
                         "replies": cfg.resolved_replies_provider(),
                         # build_notifier never posts from an offline run
                         "telegram": "on" if cfg.telegram_enabled and not cfg.offline
                                     else "off"},
            "mode": {"offline": bool(cfg.offline), "dry_run": bool(cfg.dry_run),
                     "send_only_to": list(cfg.send_only_to or []),
                     "db": str(cfg.db_path), "limit": self.limit},
            "upcoming": [dict(r) for r in upcoming],
            "runs": [dict(r) for r in runs],
            "task": self.task.to_json() if self.task else None,
            "outbox": self._outbox_brief(db),
        }

    def _outbox_brief(self, db) -> dict:
        info = outbox.status(self.cfg, db)
        return {"paced": info["paced"], "paused": info["paused"],
                "queued": len(info["queued"]), "sent_today": info["sent_today"],
                "cap_today": info["cap_today"], "bounces_7d": info["bounces_7d"],
                "sends_7d": info["sends_7d"]}

    def drafts(self, db, body) -> dict:
        return {"drafts": [draft_json(r) for r in store.pending_drafts(db)]}

    def prospects(self, db, body) -> dict:
        rows = db.query(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM drafts d
                WHERE d.prospect_id = p.id AND d.verdict IS NULL) AS pending,
              (SELECT label FROM replies r WHERE r.prospect_id = p.id
                ORDER BY r.id DESC LIMIT 1) AS reply,
              EXISTS (SELECT 1 FROM do_not_contact dnc
                       WHERE dnc.email = lower(trim(p.email))) AS blocked,
              (SELECT reason FROM do_not_contact dnc
                WHERE dnc.email = lower(trim(p.email))) AS blocked_reason,
              (SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id
                AND o.state IN ('queued','sending')) AS in_outbox
            FROM prospects p ORDER BY p.priority ASC, p.id ASC
            """)
        return {"prospects": [dict(r) for r in rows], "statuses": list(STATUSES)}

    def prospect(self, db, body, pid: str) -> dict:
        row = db.one("SELECT * FROM prospects WHERE id=?", (pid,))
        if row is None:
            raise ApiError(404, "no such prospect: " + pid)
        drafts = db.query("SELECT d.*, p.company, p.contact_name, p.email "
                          "FROM drafts d JOIN prospects p ON p.id = d.prospect_id "
                          "WHERE d.prospect_id=? ORDER BY d.id DESC", (pid,))
        replies = db.query("SELECT id, from_addr, subject, snippet, label, confidence, "
                           "extracted_ask, received_at, action FROM replies "
                           "WHERE prospect_id=? ORDER BY id DESC", (pid,))
        sources = db.query("SELECT id, fetched_at, source_url, title, excerpt "
                           "FROM research WHERE prospect_id=? ORDER BY id DESC LIMIT 12",
                           (pid,))
        actions = db.query("SELECT id, run_id, created_at, kind, confidence, "
                           "human_verdict FROM actions WHERE target=? "
                           "ORDER BY id DESC LIMIT 40", (pid,))
        research = store.latest_research(db, pid)
        reason = db.one("SELECT reason FROM do_not_contact WHERE email=?",
                        ((row["email"] or "").strip().lower(),))
        sends = {r["draft_id"]: r for r in db.query(
            "SELECT * FROM outbox WHERE prospect_id=?", (pid,))}
        drafts_out = []
        for r in drafts:
            item = draft_json(r)
            send = sends.get(r["id"])
            if send is not None and not r["sent_at"]:
                # Approved but not sent: waiting for its slot, or stopped.
                item["send_state"] = send["state"]
                item["send_note"] = (outbox.describe_wait(self.cfg, send)
                                     if send["state"] in ("queued", "sending")
                                     else send["reason"] or "")
            drafts_out.append(item)
        return {"prospect": dict(row), "blocked": store.is_blocked(db, row["email"]),
                "blocked_reason": reason["reason"] if reason else "",
                "drafts": drafts_out,
                "replies": [dict(r) for r in replies],
                "sources": [dict(r) for r in sources],
                "research": research.to_json() if research else None,
                "actions": [dict(r) for r in actions]}

    def log(self, db, body) -> dict:
        runs = db.query("SELECT id, job, status, steps, cost_usd, summary, error, "
                        "started_at, finished_at FROM runs ORDER BY id DESC LIMIT 30")
        return {"sent": [dict(r) for r in store.outreach_log(db)],
                "runs": [dict(r) for r in runs]}

    def export(self, db, body) -> Raw:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospects.csv"
            store.export_csv(db, path)
            return Raw(path.read_bytes(), "text/csv; charset=utf-8", "prospects.csv")

    def task_status(self, db, body) -> dict:
        return {"task": self.task.to_json() if self.task else None}

    def outbox_status(self, db, body) -> dict:
        info = outbox.status(self.cfg, db)
        for row in info["queued"]:
            row["wait"] = outbox.describe_wait(self.cfg, row)
        return info

    def outbox_pause(self, db, body) -> dict:
        reason = " ".join(str(body.get("reason") or "").split())[:200]
        outbox.pause(db, reason or "paused from the web page")
        return {"message": "sending paused", "outbox": self.outbox_status(db, {})}

    def outbox_resume(self, db, body) -> dict:
        outbox.resume(db)
        return {"message": "sending resumed", "outbox": self.outbox_status(db, {})}

    def outbox_send(self, db, body) -> dict:
        results = outbox.dispatch(self.cfg, db, build_notifier(self.cfg), self.event,
                                  self.voice)
        sent = sum(1 for r in results if r.get("sent"))
        return {"message": "{} sent now".format(sent) if sent else
                "nothing was due; the queue keeps its pace",
                "outbox": self.outbox_status(db, {})}

    def outbox_cancel(self, db, body, outbox_id: str) -> dict:
        if not outbox.cancel(db, int(outbox_id), "cancelled from the web page"):
            raise ApiError(409, "#{} is not waiting in the outbox".format(outbox_id))
        return {"message": "cancelled #{}; it will not be sent".format(outbox_id),
                "outbox": self.outbox_status(db, {})}

    # -- verdicts ----------------------------------------------------------
    def verdict(self, db, body, draft_id: str, verdict: str) -> dict:
        if verdict not in VERDICTS:
            raise ApiError(400, "unknown verdict: " + verdict)
        text = ""
        if verdict == "edit":
            subject = " ".join((body.get("subject") or "").split())
            text = (body.get("body") or "").strip()
            if not text:
                raise ApiError(400, "the corrected email is empty; nothing changed")
            if subject:
                text = "Subject: {}\n{}".format(subject, text)
        elif verdict == "redo":
            text = (body.get("instruction") or "").strip()
            if not text:
                raise ApiError(400, "say what should change; nothing changed")
        with self._exclusive():
            if store.get_draft(db, int(draft_id)) is None:
                raise ApiError(404, "no draft {}".format(draft_id))
            notifier = build_notifier(self.cfg)
            self._job(db, notifier)
            message = approvals.apply_verdict(
                self.cfg, db, notifier,
                "{}:{}:{}".format(approve.PREFIX, draft_id, verdict), text)
        row = store.get_draft(db, int(draft_id))
        return {"message": message, "draft": draft_json(row) if row else None}

    # -- prospects ---------------------------------------------------------
    def add(self, db, body) -> dict:
        company = " ".join((body.get("company") or "").split())
        if not company:
            raise ApiError(400, "company name is required")
        email = _clean_email(body.get("email"))
        existing = store.find_by_company(db, company)
        if existing:
            if email and not existing.email:
                self._write_email(db, existing.id, email)
                return {"id": existing.id, "message": "{} is already listed as {}; "
                        "address added.".format(company, existing.id)}
            return {"id": existing.id, "message": "{} is already on the list as {}."
                    .format(company, existing.id)}
        try:
            pid = store.add_prospect(
                db, company, email=email,
                contact_name=body.get("contact_name") or "",
                segment=body.get("segment") or "", notes=body.get("notes") or "",
                website=body.get("website") or "", source="web")
        except sqlite3.IntegrityError:
            raise ApiError(409, "{} already belongs to another prospect".format(email))
        return {"id": pid, "message": "added {} {}{}".format(
            pid, company, "" if email else " (no address yet)")}

    def add_many(self, db, body) -> dict:
        """The [Add selected] after a discover: what `discover --apply` writes."""
        added = []
        for lead in (body.get("leads") or [])[:50]:
            company = " ".join(str((lead or {}).get("company") or "").split())
            if not company or store.find_by_company(db, company):
                continue
            pid = store.add_prospect(db, company, website=lead.get("website") or "",
                                     source="discover", notes=lead.get("notes") or "")
            added.append({"id": pid, "company": company})
        return {"added": added, "message": "added {} prospect(s), no address yet"
                .format(len(added))}

    def _write_email(self, db, pid: str, email: str, contact_name=None,
                     source: str = "web") -> None:
        try:
            store.set_email(db, pid, email, contact_name=contact_name, source_url=source)
        except sqlite3.IntegrityError:
            raise ApiError(409, "{} already belongs to another prospect".format(email))

    def set_email(self, db, body, pid: str) -> dict:
        prospect = store.get(db, pid)
        if prospect is None:
            raise ApiError(404, "no such prospect: " + pid)
        email = _clean_email(body.get("email"))
        if not email:
            raise ApiError(400, "an address is required")
        self._write_email(db, pid, email, contact_name=body.get("contact_name"),
                          source=body.get("source_url") or "web")
        note = " (on the do-not-contact list: nothing will be drafted)" \
            if store.is_blocked(db, email) else ""
        return {"message": "{} -> {}{}".format(pid, email, note)}

    def never(self, db, body, pid: str) -> dict:
        if store.get(db, pid) is None:
            raise ApiError(404, "no such prospect: " + pid)
        n = store.mark_never(db, pid, reason="never, from the web page")
        return {"message": "{} marked declined permanently; {} pending draft(s) "
                           "cancelled.".format(pid, n)}

    # -- agent tasks -------------------------------------------------------
    def start_task(self, db, body, name: str) -> dict:
        if name not in TASKS:
            raise ApiError(404, "no such task: " + name)
        limit = _int(body.get("limit"), TASKS[name] or self.limit)
        if not self.busy.acquire(blocking=False):
            raise ApiError(409, self._busy_message())
        self._task_ids += 1
        task = Task(id=self._task_ids, name=name, started_at=utcnow())
        self.task = task
        try:
            threading.Thread(target=self._run_task, args=(task, limit), daemon=True,
                             name="sponsy-" + name).start()
        except Exception:
            self.busy.release()
            raise
        return {"task": task.to_json()}

    def _run_task(self, task: Task, limit: int) -> None:
        """Owns `busy` (taken by start_task) until it finishes."""
        if self.tee is not None:
            self.tee.sinks[threading.get_ident()] = task.log
        db = Database(self.cfg.db_path)
        try:
            fn = getattr(self, "_task_" + task.name.replace("-", "_"))
            task.result = fn(db, limit)
            task.state = "done"
        except LLMError as exc:
            task.state, task.error = "failed", "stopped: {}".format(exc)
        except Exception as exc:                    # noqa: BLE001
            traceback.print_exc()
            task.state, task.error = "failed", "{}: {}".format(type(exc).__name__, exc)
        finally:
            task.finished_at = utcnow()
            db.close()
            if self.tee is not None:
                self.tee.sinks.pop(threading.get_ident(), None)
            self.busy.release()

    def _task_run(self, db, limit: int) -> dict:
        if not store.all_prospects(db):
            csv_path = DATA / "prospects.csv"
            if csv_path.exists():
                report = store.import_csv(db, csv_path)
                print("[web] imported {} prospect(s) from {}".format(
                    report["added"], csv_path))
        job = self._job(db, build_notifier(self.cfg), limit=limit)
        result = job.run()
        if result is None:
            return {"ok": True, "summary": "did not run", "drafts": 0}
        return {"ok": result.ok, "summary": result.summary,
                "confidence": result.confidence, "steps": result.steps,
                "cost_usd": result.cost_usd, "elapsed_s": result.elapsed_s,
                "drafts": len(result.actions), "reason": result.reason,
                "unverified": [a["prospect_id"] for a in result.actions
                               if not a["verified"]]}

    def _task_discover(self, db, limit: int) -> dict:
        from . import prospecting  # noqa: PLC0415
        print("reading sponsor pages of comparable events via {} ...".format(
            self.cfg.resolved_search_provider()))
        leads = prospecting.discover(self.cfg, build_llm(self.cfg, db=db), self.event,
                                     db=db)
        fresh = [x for x in leads if not prospecting.already_known(db, x.company)]
        return {"found": len(leads), "fresh": len(fresh), "leads": [
            {"company": lead.company, "website": lead.website,
             "strength": lead.strength, "sources": list(lead.sources[:3]),
             "events": sorted(str(e) for e in lead.events)[:5],
             "notes": "sponsors comparable events; seen on {} sponsor page(s), "
                      "e.g. {}".format(lead.strength, lead.sources[0])
             if lead.sources else ""}
            for lead in fresh[:limit]]}

    def _task_find_contacts(self, db, limit: int) -> dict:
        from . import contacts  # noqa: PLC0415
        waiting = store.awaiting_contact(db)
        todo = [p for p in (store.get(db, pid) for pid in waiting) if p][:limit]
        print("searching for published addresses for {} prospect(s) via {}".format(
            len(todo), self.cfg.resolved_search_provider()))
        findings = []
        for prospect in todo:
            print("{}  {}".format(prospect.id, prospect.company))
            finding = contacts.find_for(self.cfg, prospect)
            findings.append({
                "prospect_id": prospect.id, "company": prospect.company,
                "candidates": [{"email": c.email, "source_url": c.source_url,
                                "score": c.score, "note": c.note}
                               for c in finding.candidates[:3]],
                "form_urls": list(finding.form_urls[:2])})
        return {"findings": findings, "remaining": max(0, len(waiting) - len(todo))}

    def _task_doctor(self, db, limit: int) -> dict:
        from . import doctor  # noqa: PLC0415
        checks = doctor.run_checks(self.cfg, event=self.event, voice=self.voice,
                                   live=not self.cfg.offline)
        return {"checks": [{"name": c.name, "ok": c.ok, "status": c.status,
                            "detail": c.detail, "hint": c.hint,
                            "optional": c.optional} for c in checks]}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sponsy"

        def log_message(self, fmt, *args) -> None:     # the page polls; stay quiet
            pass

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def _read_body(self) -> bytes | None:
            """The request body, read BEFORE any reply. Answering a refusal
            with the body still unread makes the close a TCP reset on
            Windows, and the client sees "connection aborted" instead of
            the 4xx. None means too large to read (the reply is 413)."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self.close_connection = True
                return None
            return self.rfile.read(length) if length > 0 else b""

        def _serve(self, method: str) -> None:
            path = urlsplit(self.path).path
            host = self.headers.get("Host", "")
            raw = b""
            if method == "POST":
                raw = self._read_body()
                if raw is None:
                    return self._json(413, {"error": "request too large"})
            # Public by design (see the module docstring): checked before the
            # Host, token and JSON rules, which a mail provider cannot meet.
            if path == "/healthz" and method == "GET":
                status, payload = app.health()
                return self._json(status, payload)
            match = UNSUB_RE.fullmatch(path)
            if match:
                form = raw[:4096].decode("utf-8", "replace")
                status, data, ctype = app.unsubscribe(method, match.group(1),
                                                      match.group(2), form)
                return self._send(status, data, ctype, csp=PUBLIC_CSP)
            if not app.host_ok(host):
                return self._json(400, {"error": "unexpected Host header"})
            if app.via_public(host) and not app.token:
                # Came in through the public proxy, and nothing guards the UI:
                # a Caddyfile that forwards everything must not publish it.
                return self._json(403, {"error": "the UI is not served on the public "
                                                 "address; use an SSH tunnel"})
            if not path.startswith("/api/"):
                if method != "GET":
                    return self._json(405, {"error": "method not allowed"})
                return self._static(path)
            if not app.authorized(self.headers.get("X-Sponsy-Token", "")):
                return self._json(401, {"error": "token required"})
            body: dict = {}
            if method == "POST":
                # JSON only: a plain cross-site <form> cannot send it, and the
                # Origin check stops a page that tries harder.
                ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
                if ctype != "application/json":
                    return self._json(415, {"error": "send application/json"})
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc.lower() != host.lower():
                    return self._json(403, {"error": "cross-origin request refused"})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"error": "invalid JSON"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "expected a JSON object"})
            status, payload = app.handle(method, path, body)
            if isinstance(payload, Raw):
                return self._send(status, payload.body, payload.content_type,
                                  payload.filename)
            return self._json(status, payload)

        def _static(self, path: str) -> None:
            name = path.lstrip("/") or "index.html"
            target = (STATIC / name).resolve()
            if (target.parent != STATIC or not target.is_file()
                    or target.suffix not in CONTENT_TYPES):
                return self._json(404, {"error": "not found"})
            self._send(200, target.read_bytes(), CONTENT_TYPES[target.suffix])

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _send(self, status: int, data: bytes, content_type: str,
                  filename: str = "", csp: str = CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            if filename:
                self.send_header("Content-Disposition",
                                 'attachment; filename="{}"'.format(filename))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(app: WebApp, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = False) -> None:
    """Until ctrl-c."""
    app.loopback = is_loopback(host)
    if not app.loopback and not app.token:
        raise SystemExit("refusing to listen on {} without a token: this page can "
                         "send email. Set SPONSY_WEB_TOKEN (or --token).".format(host))
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    app.tee = _Tee(sys.stdout)
    sys.stdout = app.tee
    threading.Thread(target=app.dispatch_loop, daemon=True,
                     name="sponsy-outbox").start()
    # docker stop / systemd send SIGTERM: stop serving, but only once an
    # email the outbox thread is handing to the provider has been recorded.
    try:
        import signal  # noqa: PLC0415
        import time  # noqa: PLC0415

        def _drain_then_stop() -> None:
            while outbox.sending():
                time.sleep(0.2)
            httpd.shutdown()

        def _term(signum, frame):
            app.stop.set()
            threading.Thread(target=_drain_then_stop, daemon=True).start()
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError, AttributeError):
        pass
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    url = "http://{}:{}/".format(shown, httpd.server_port)
    print("[web] {}  (db {}; mail {}; ctrl-c to stop)".format(
        url, app.cfg.db_path, app.cfg.resolved_mail_provider()))
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] stopped")
    finally:
        app.stop.set()
        httpd.server_close()
        sys.stdout = app.tee.stream
