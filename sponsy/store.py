"""Tables, and the rules that are too important to leave to a prompt.

Eight rules are enforced by SQLite triggers rather than by agent code:

  * no send without a human verdict on the draft
  * declined is permanent
  * never more than 4 cold touches
  * no draft for a closed prospect
  * no draft to an address that asked us to stop
  * a draft can only be marked sent once
  * nothing enters the outbox without a human verdict
  * nothing leaves the outbox for an address on the do-not-contact list

If every line of Python in this repo were wrong, those would still hold.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable

from .state import Database, utcnow
from . import ladder
from . import models
from .models import (CLOSED, Draft, Excerpt, IllegalTransition, Prospect, Research,
                     check_transition, norm_company)

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  id              TEXT PRIMARY KEY,
  company         TEXT NOT NULL,
  contact_name    TEXT,
  email           TEXT,
  segment         TEXT,
  status          TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new','researched','queued','sent','replied',
                                    'interested','declined','cold')),
  first_touch     TEXT,
  last_touch      TEXT,
  next_touch      TEXT,
  thread_id       TEXT,
  gmail_thread_id TEXT,
  touches         INTEGER NOT NULL DEFAULT 0 CHECK (touches BETWEEN 0 AND 4),
  notes           TEXT,
  website         TEXT,
  source          TEXT,
  -- Lower runs first. Set from the ranked list so the warm leads are worked
  -- before the long shots, instead of whatever order the CSV happened to be in.
  priority        INTEGER NOT NULL DEFAULT 100,
  created_at      TEXT
);

CREATE TABLE IF NOT EXISTS research (
  id          INTEGER PRIMARY KEY,
  prospect_id TEXT REFERENCES prospects(id),
  run_id      INTEGER,
  fetched_at  TEXT,
  source_url  TEXT,
  title       TEXT,
  excerpt     TEXT                      -- the ONLY facts the drafter may use
);

CREATE TABLE IF NOT EXISTS research_summary (
  id          INTEGER PRIMARY KEY,
  prospect_id TEXT REFERENCES prospects(id),
  run_id      INTEGER,
  created_at  TEXT,
  payload     TEXT,
  confidence  REAL
);

CREATE TABLE IF NOT EXISTS drafts (
  id            INTEGER PRIMARY KEY,
  prospect_id   TEXT REFERENCES prospects(id),
  run_id        INTEGER,
  action_id     INTEGER,
  kind          TEXT CHECK (kind IN ('initial','returning','nudge','new_angle',
                                     'last_touch','needs_info')),
  subject       TEXT,
  body          TEXT,
  claims        TEXT,
  checks        TEXT,
  verified      INTEGER DEFAULT 0,
  confidence    REAL,
  humanized     INTEGER DEFAULT 0,
  ai_tell_score REAL,
  supersedes    INTEGER,
  tg_message_id INTEGER,
  created_at    TEXT,
  verdict       TEXT CHECK (verdict IN ('approved','edited','skipped','rejected',
                                        'superseded')),
  sent_at       TEXT,
  message_id    TEXT,
  channel       TEXT
);

CREATE TABLE IF NOT EXISTS replies (
  id            INTEGER PRIMARY KEY,
  prospect_id   TEXT REFERENCES prospects(id),
  uid           TEXT,
  message_id    TEXT,
  from_addr     TEXT,
  subject       TEXT,
  snippet       TEXT,
  label         TEXT,
  confidence    REAL,
  extracted_ask TEXT,
  received_at   TEXT,
  handled_at    TEXT,
  action        TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
  id             INTEGER PRIMARY KEY,
  draft_id       INTEGER REFERENCES drafts(id),
  prospect_id    TEXT,
  kind           TEXT,
  before_subject TEXT, before_body TEXT,
  after_subject  TEXT, after_body  TEXT,
  instruction    TEXT,
  created_at     TEXT
);

CREATE TABLE IF NOT EXISTS do_not_contact (
  email      TEXT PRIMARY KEY,
  reason     TEXT,
  created_at TEXT
);

-- Approved emails waiting for the send-rate policy (sponsy/outbox.py). A
-- row is the promise "this goes out when pacing allows"; drafts.sent_at is
-- still the only record that it actually did.
CREATE TABLE IF NOT EXISTS outbox (
  id          INTEGER PRIMARY KEY,
  draft_id    INTEGER NOT NULL UNIQUE REFERENCES drafts(id),
  prospect_id TEXT REFERENCES prospects(id),
  to_addr     TEXT,
  state       TEXT NOT NULL DEFAULT 'queued'
              CHECK (state IN ('queued','sending','sent','failed','cancelled')),
  queued_at   TEXT,
  not_before  TEXT,                     -- UTC ISO; the earliest it may leave
  attempts    INTEGER NOT NULL DEFAULT 0,
  message_id  TEXT,                     -- fixed on the first attempt, reused on retry
  eml         TEXT,
  channel     TEXT,
  reason      TEXT,                     -- why it is waiting, or why it stopped
  last_error  TEXT,
  updated_at  TEXT,
  sent_at     TEXT
);

-- Cross-process leases: `serve` and `web` may both run against one database,
-- and only one of them may be sending at a time.
CREATE TABLE IF NOT EXISTS locks (
  name       TEXT PRIMARY KEY,
  owner      TEXT,
  expires_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_outbox_due        ON outbox(state, not_before);
CREATE INDEX IF NOT EXISTS ix_research_prospect ON research(prospect_id);
CREATE INDEX IF NOT EXISTS ix_drafts_prospect   ON drafts(prospect_id);
CREATE INDEX IF NOT EXISTS ix_drafts_pending    ON drafts(verdict) WHERE verdict IS NULL;
CREATE INDEX IF NOT EXISTS ix_prospects_due     ON prospects(status, next_touch);
CREATE UNIQUE INDEX IF NOT EXISTS ux_prospects_email
  ON prospects(lower(email)) WHERE email IS NOT NULL AND trim(email) <> '';

-- ---------------------------------------------------------------- guardrails
CREATE TRIGGER IF NOT EXISTS trg_declined_is_permanent
BEFORE UPDATE OF status ON prospects
WHEN OLD.status = 'declined' AND NEW.status <> 'declined'
BEGIN SELECT RAISE(ABORT, 'declined is permanent'); END;

CREATE TRIGGER IF NOT EXISTS trg_touch_cap
BEFORE UPDATE OF touches ON prospects
WHEN NEW.touches > 4
BEGIN SELECT RAISE(ABORT, 'touch cap: 4 touches maximum'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_draft_past_cap
BEFORE INSERT ON drafts
WHEN NEW.kind IN ('initial','returning','nudge','new_angle','last_touch')
 AND (SELECT touches FROM prospects WHERE id = NEW.prospect_id) >= 4
BEGIN SELECT RAISE(ABORT, 'touch cap reached: no further outreach drafts'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_draft_when_closed
BEFORE INSERT ON drafts
WHEN (SELECT status FROM prospects WHERE id = NEW.prospect_id)
     IN ('declined','cold','interested')
BEGIN SELECT RAISE(ABORT, 'prospect is closed: no drafts'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_draft_do_not_contact
BEFORE INSERT ON drafts
WHEN EXISTS (SELECT 1 FROM do_not_contact
             WHERE email = (SELECT lower(trim(email)) FROM prospects
                            WHERE id = NEW.prospect_id))
BEGIN SELECT RAISE(ABORT, 'address is on the do-not-contact list'); END;

CREATE TRIGGER IF NOT EXISTS trg_send_needs_approval
BEFORE UPDATE OF sent_at ON drafts
WHEN NEW.sent_at IS NOT NULL
 AND (NEW.verdict IS NULL OR NEW.verdict NOT IN ('approved','edited'))
BEGIN SELECT RAISE(ABORT, 'no send without a human verdict'); END;

CREATE TRIGGER IF NOT EXISTS trg_send_only_once
BEFORE UPDATE OF sent_at ON drafts
WHEN OLD.sent_at IS NOT NULL AND NEW.sent_at IS NOT OLD.sent_at
BEGIN SELECT RAISE(ABORT, 'draft was already sent'); END;

CREATE TRIGGER IF NOT EXISTS trg_outbox_needs_approval
BEFORE INSERT ON outbox
WHEN COALESCE((SELECT verdict FROM drafts WHERE id = NEW.draft_id), '')
     NOT IN ('approved','edited')
BEGIN SELECT RAISE(ABORT, 'no queued send without a human verdict'); END;

CREATE TRIGGER IF NOT EXISTS trg_outbox_do_not_contact
BEFORE UPDATE OF state ON outbox
WHEN NEW.state = 'sending'
 AND EXISTS (SELECT 1 FROM do_not_contact WHERE email = lower(trim(NEW.to_addr)))
BEGIN SELECT RAISE(ABORT, 'address is on the do-not-contact list'); END;
"""

# Columns added after the first release. ensure_schema() adds any that are
# missing, so a database created by an older version keeps working.
LATER_COLUMNS = {
    "prospects": {"gmail_thread_id": "TEXT", "website": "TEXT", "source": "TEXT",
                  "priority": "INTEGER NOT NULL DEFAULT 100"},
    "drafts": {"humanized": "INTEGER DEFAULT 0", "ai_tell_score": "REAL",
               "supersedes": "INTEGER", "tg_message_id": "INTEGER",
               "channel": "TEXT"},
    "corrections": {"instruction": "TEXT"},
}

CSV_COLUMNS = ("prospect_id", "company", "contact_name", "email", "segment",
               "status", "website", "notes")
EXPORT_COLUMNS = ("prospect_id", "company", "contact_name", "email", "segment",
                  "status", "touches", "first_touch", "last_touch", "next_touch",
                  "website", "source", "notes")


def _add_later_columns(db: Database, table: str) -> None:
    have = db.columns(table)
    for name, decl in LATER_COLUMNS.get(table, {}).items():
        if name not in have:
            db.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, decl))


def _drafts_ddl(name: str) -> str:
    """The CREATE TABLE for drafts, lifted out of SCHEMA under a new name.

    Extracted rather than duplicated so the migration below cannot drift away
    from the real schema the way a hand-copied DDL always eventually does.
    """
    match = re.search(r"CREATE TABLE IF NOT EXISTS drafts \(.*?\n\);", SCHEMA, re.S)
    if match is None:                       # pragma: no cover -- SCHEMA is a literal
        raise RuntimeError("drafts DDL not found in SCHEMA")
    return match.group(0).replace("CREATE TABLE IF NOT EXISTS drafts (",
                                  "CREATE TABLE {} (".format(name), 1)


def migrate_draft_kinds(db: Database) -> bool:
    """Widen the drafts.kind CHECK constraint when it predates `returning`.

    SQLite cannot ALTER a CHECK, so the table is rebuilt. Returns True when a
    rebuild happened. Dropping drafts also drops the triggers and indexes on
    it, so the caller re-runs SCHEMA afterwards to put them back -- without
    that, the approval gate would silently disappear, which is the one failure
    this project cannot have.
    """
    row = db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='drafts'")
    if row is None or "'returning'" in (row["sql"] or ""):
        return False

    old = db.columns("drafts")
    db.execute("PRAGMA foreign_keys=off")   # a PRAGMA inside a transaction is a no-op
    try:
        with db.transaction():
            db.execute("DROP TABLE IF EXISTS drafts_migrate")
            db.execute(_drafts_ddl("drafts_migrate"))
            _add_later_columns(db, "drafts_migrate")
            shared = [c for c in db.columns("drafts_migrate") if c in old]
            cols = ", ".join(shared)
            db.execute("INSERT INTO drafts_migrate ({}) SELECT {} FROM drafts"
                       .format(cols, cols))
            # A trigger on another table that reads drafts would make the
            # RENAME below fail while drafts does not exist; SCHEMA puts it back.
            db.execute("DROP TRIGGER IF EXISTS trg_outbox_needs_approval")
            db.execute("DROP TABLE drafts")
            db.execute("ALTER TABLE drafts_migrate RENAME TO drafts")
    finally:
        db.execute("PRAGMA foreign_keys=on")
    db.executescript(SCHEMA)                # restore the dropped triggers/indexes
    return True


def ensure_schema(db: Database) -> None:
    # One transaction: one sync to disk instead of one per CREATE.
    db.executescript("BEGIN;\n" + SCHEMA + "\nCOMMIT;")
    for table in LATER_COLUMNS:
        _add_later_columns(db, table)
    migrate_draft_kinds(db)


# ---------------------------------------------------------------------------
# prospects
# ---------------------------------------------------------------------------

# norm_company is defined in models (so config.Event can use it without a
# cycle) and imported at the top of this file, because this is where callers
# have always imported it from.


def import_csv(db: Database, path: str | Path) -> dict:
    """Upsert prospects. Contact details are refreshed; state never is.

    A blank cell refreshes nothing: it keeps what the database already has.
    The addresses that `find-contacts --apply`, `add` and `/add` find are
    written to the database only, and most rows of the list have an empty
    email cell, so overwriting with the blank would quietly park every one of
    them again. To change a value, put the new one in the cell.
    """
    path = Path(path)
    added = updated = skipped = 0
    problems: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            pid = row.get("prospect_id") or row.get("id")
            company = row.get("company")
            if not pid or not company:
                skipped += 1
                problems.append("row without prospect_id/company: {}".format(row))
                continue
            existing = db.one("SELECT id FROM prospects WHERE id=?", (pid,))
            try:
                if existing:
                    db.execute(
                        "UPDATE prospects SET company=?, "
                        "contact_name=COALESCE(NULLIF(?,''), contact_name), "
                        "email=COALESCE(NULLIF(?,''), email), "
                        "segment=COALESCE(NULLIF(?,''), segment), "
                        "website=COALESCE(NULLIF(?,''), website), "
                        "notes=COALESCE(NULLIF(?,''), notes) WHERE id=?",
                        (company, row.get("contact_name", ""), row.get("email", ""),
                         row.get("segment", ""), row.get("website", ""),
                         row.get("notes", ""), pid))
                    updated += 1
                else:
                    status = row.get("status") or "new"
                    db.execute(
                        "INSERT INTO prospects (id, company, contact_name, email, "
                        "segment, status, last_touch, thread_id, notes, website, "
                        "source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, company, row.get("contact_name", ""),
                         row.get("email", ""), row.get("segment", ""), status,
                         row.get("last_touch") or None, row.get("thread_id") or None,
                         row.get("notes", ""), row.get("website", ""),
                         row.get("source") or "csv", utcnow()))
                    added += 1
            except sqlite3.IntegrityError as exc:
                skipped += 1
                problems.append("{}: {}".format(pid, exc))
    return {"added": added, "updated": updated, "skipped": skipped,
            "problems": problems}


def export_csv(db: Database, path: str | Path) -> int:
    path = Path(path)
    rows = db.query("SELECT * FROM prospects ORDER BY id")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            data = dict(row)
            data["prospect_id"] = data["id"]
            writer.writerow([data.get(c) if data.get(c) is not None else ""
                             for c in EXPORT_COLUMNS])
    return len(rows)


def next_id(db: Database, prefix: str = "S") -> str:
    rows = db.query("SELECT id FROM prospects WHERE id LIKE ?", (prefix + "%",))
    best = 0
    for row in rows:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", row["id"])
        if match:
            best = max(best, int(match.group(1)))
    return "{}{:03d}".format(prefix, best + 1)


def find_by_company(db: Database, company: str) -> Prospect | None:
    target = norm_company(company)
    if not target:
        return None
    for p in all_prospects(db):
        if norm_company(p.company) == target:
            return p
    return None


def add_prospect(db: Database, company: str, email: str = "", contact_name: str = "",
                 segment: str = "", notes: str = "", website: str = "",
                 source: str = "manual", pid: str | None = None,
                 priority: int = 100) -> str:
    """Add one prospect. Returns the id; if the company is already on the list,
    returns the existing id without changing anything."""
    company = (company or "").strip()
    if not company:
        raise ValueError("company name is required")
    existing = find_by_company(db, company)
    if existing:
        return existing.id
    pid = pid or next_id(db)
    db.execute(
        "INSERT INTO prospects (id, company, contact_name, email, segment, status, "
        "notes, website, source, priority, created_at) VALUES (?,?,?,?,?,'new',?,?,?,?,?)",
        (pid, company, contact_name.strip(), email.strip().lower(), segment.strip(),
         notes.strip(), website.strip(), source, int(priority), utcnow()))
    return pid


def get(db: Database, pid: str) -> Prospect | None:
    row = db.one("SELECT * FROM prospects WHERE id=?", (pid,))
    return Prospect.from_row(row) if row else None


def all_prospects(db: Database) -> list[Prospect]:
    return [Prospect.from_row(r) for r in
            db.query("SELECT * FROM prospects ORDER BY id")]


def due_today(db: Database, today: date, limit: int = 10) -> list[Prospect]:
    """new prospects, plus anyone whose next_touch has come due.

    Excludes closed prospects, anyone at the touch cap, and -- importantly for a
    daily cron -- anyone who already has a draft waiting for a verdict.
    """
    rows = db.query(
        """
        SELECT p.* FROM prospects p
        WHERE p.status NOT IN ('declined','cold','interested','replied')
          AND p.touches < ?
          -- No address, no point: researching and drafting for someone we
          -- cannot write to just spends model quota. They wait here until you
          -- add a contact.
          AND trim(COALESCE(p.email, '')) <> ''
          AND (p.next_touch IS NULL OR p.next_touch <= ?)
          AND NOT EXISTS (SELECT 1 FROM drafts d
                          WHERE d.prospect_id = p.id AND d.verdict IS NULL)
          -- ...and so is an approved email still waiting in the outbox.
          AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.prospect_id = p.id
                          AND o.state IN ('queued','sending'))
          AND NOT EXISTS (SELECT 1 FROM do_not_contact dnc
                          WHERE dnc.email = lower(trim(p.email)))
        ORDER BY p.priority ASC, (p.next_touch IS NULL) DESC,
                 p.next_touch ASC, p.id ASC
        LIMIT ?
        """,
        (ladder.MAX_TOUCHES, today.isoformat(), limit))
    return [Prospect.from_row(r) for r in rows]


def set_status(db: Database, pid: str, status: str) -> None:
    current = db.one("SELECT status FROM prospects WHERE id=?", (pid,))
    if current is None:
        raise KeyError(pid)
    check_transition(current["status"], status)
    db.execute("UPDATE prospects SET status=? WHERE id=?", (status, pid))


def set_next_touch(db: Database, pid: str, when: str | None) -> None:
    db.execute("UPDATE prospects SET next_touch=? WHERE id=?", (when, pid))


def set_email(db: Database, pid: str, email: str, contact_name: str | None = None,
              source_url: str = "") -> None:
    email = (email or "").strip().lower()
    if contact_name is not None:
        db.execute("UPDATE prospects SET email=?, contact_name=? WHERE id=?",
                   (email, contact_name.strip(), pid))
    else:
        db.execute("UPDATE prospects SET email=? WHERE id=?", (email, pid))
    db.log_action(None, "contact_set", pid, {"email": email, "source": source_url})


def sweep_cold(db: Database, today: date) -> list[str]:
    """Anyone past day 21 with no reply gets marked cold and stops."""
    gone: list[str] = []
    rows = db.query(
        "SELECT id, first_touch FROM prospects "
        "WHERE status IN ('sent','queued','researched') AND first_touch IS NOT NULL")
    for row in rows:
        if ladder.is_cold(row["first_touch"], today):
            if db.one("SELECT 1 FROM drafts WHERE prospect_id=? AND verdict IS NULL",
                      (row["id"],)):
                continue                       # a draft is still awaiting a verdict
            if has_queued_send(db, row["id"]):
                continue                       # an approved email has not left yet
            set_status(db, row["id"], "cold")
            set_next_touch(db, row["id"], None)
            gone.append(row["id"])
    return gone


def block_email(db: Database, email: str, reason: str) -> None:
    if not email:
        return
    db.execute("INSERT OR REPLACE INTO do_not_contact (email, reason, created_at) "
               "VALUES (?,?,?)", (email.strip().lower(), reason, utcnow()))


def is_blocked(db: Database, email: str) -> bool:
    if not email:
        return False
    return db.one("SELECT 1 FROM do_not_contact WHERE email=?",
                  (email.strip().lower(),)) is not None


def mark_never(db: Database, pid: str, reason: str = "marked never by a human") -> int:
    """[Never]: declined, permanently, and the address is blocked. Returns the
    number of pending drafts that were cancelled."""
    prospect = get(db, pid)
    if prospect is None:
        raise KeyError(pid)
    cancelled = cancel_pending(db, pid, reason)
    set_next_touch(db, pid, None)
    block_email(db, prospect.email, reason)
    if prospect.status != "declined":
        set_status(db, pid, "declined")
    db.log_action(None, "never", pid, {"reason": reason}, confidence=1.0)
    return cancelled


def awaiting_contact(db: Database) -> list[str]:
    """Prospects parked because nobody has found a usable address for them:
    none at all, or one that bounced."""
    return [r["id"] for r in db.query(
        "SELECT p.id FROM prospects p WHERE "
        "(trim(COALESCE(p.email,'')) = '' OR EXISTS (SELECT 1 FROM do_not_contact d "
        " WHERE d.email = lower(trim(p.email)) AND d.reason LIKE 'bounce%')) "
        "AND p.status NOT IN ('declined','cold','interested') "
        "ORDER BY p.priority ASC, p.id ASC")]


def bounced(db: Database, pid: str, email: str, reason: str,
            return_touch: bool = True) -> dict:
    """A hard bounce: block the address, stop the ladder, and -- when the
    only email this prospect ever got was the one that bounced -- give the
    touch back, so a corrected address starts at touch 1 instead of a
    "following up on my last email" to someone who never saw it.

    `reason` should start with "bounce" so awaiting_contact can tell a dead
    address from a person who asked us to stop."""
    prospect = get(db, pid)
    email = (email or "").strip().lower()
    block_email(db, email, reason)
    if prospect is None:
        return {"cancelled": 0, "reset": False}
    cancelled = cancel_pending(db, pid, "address bounced")
    set_next_touch(db, pid, None)
    reset = False
    if (return_touch and prospect.touches == 1
            and (prospect.email or "").strip().lower() == email):
        db.execute("UPDATE prospects SET touches=0, first_touch=NULL, last_touch=NULL, "
                   "thread_id=NULL, gmail_thread_id=NULL WHERE id=?", (pid,))
        reset = True
    db.log_action(None, "bounce", pid, {"email": email, "reason": reason,
                                        "touch_returned": reset}, confidence=1.0)
    return {"cancelled": cancelled, "reset": reset}


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

def save_excerpt(db: Database, pid: str, run_id: int | None, url: str,
                 title: str, text: str) -> int:
    cur = db.execute(
        "INSERT INTO research (prospect_id, run_id, fetched_at, source_url, title, "
        "excerpt) VALUES (?,?,?,?,?,?)", (pid, run_id, utcnow(), url, title, text))
    return int(cur.lastrowid)


def excerpts_for(db: Database, pid: str, limit: int = 8) -> list[Excerpt]:
    rows = db.query(
        "SELECT source_url, title, excerpt FROM research WHERE prospect_id=? "
        "ORDER BY id DESC LIMIT ?", (pid, limit))
    return [Excerpt(url=r["source_url"], title=r["title"] or "",
                    text=r["excerpt"] or "") for r in reversed(rows)]


def save_research(db: Database, research: Research, run_id: int | None) -> None:
    db.execute(
        "INSERT INTO research_summary (prospect_id, run_id, created_at, payload, "
        "confidence) VALUES (?,?,?,?,?)",
        (research.prospect_id, run_id, utcnow(),
         json.dumps(research.to_json(), ensure_ascii=False), research.confidence))


def latest_research(db: Database, pid: str) -> Research | None:
    row = db.one("SELECT payload, confidence FROM research_summary "
                 "WHERE prospect_id=? ORDER BY id DESC LIMIT 1", (pid,))
    if not row:
        return None
    return Research.from_json(pid, json.loads(row["payload"]))


def latest_research_at(db: Database, pid: str) -> str | None:
    """When we last researched this prospect (ISO, UTC), or None.

    The job uses this to decide whether a stored closed world is still worth
    reusing. Research is the expensive half of a run -- four page fetches and
    a multi-step model loop -- so re-running it for a prospect whose draft was
    merely skipped three days ago is the most wasteful thing this agent can do
    on a free-tier key.
    """
    row = db.one("SELECT created_at FROM research_summary WHERE prospect_id=? "
                 "ORDER BY id DESC LIMIT 1", (pid,))
    return row["created_at"] if row else None


# ---------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------

def insert_draft(db: Database, draft: Draft, run_id: int | None,
                 action_id: int | None = None) -> int:
    cur = db.execute(
        "INSERT INTO drafts (prospect_id, run_id, action_id, kind, subject, body, "
        "claims, checks, verified, confidence, humanized, ai_tell_score, supersedes, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (draft.prospect_id, run_id, action_id, draft.kind, draft.subject, draft.body,
         json.dumps(draft.claims, ensure_ascii=False),
         json.dumps(draft.checks, ensure_ascii=False, default=str),
         int(draft.verified), draft.confidence, int(draft.humanized),
         draft.ai_tell_score, draft.supersedes, utcnow()))
    draft.id = int(cur.lastrowid)
    return draft.id


def get_draft(db: Database, draft_id: int) -> sqlite3.Row | None:
    return db.one("SELECT d.*, p.company, p.contact_name, p.email "
                  "FROM drafts d JOIN prospects p ON p.id = d.prospect_id "
                  "WHERE d.id=?", (draft_id,))


def pending_drafts(db: Database) -> list[sqlite3.Row]:
    return db.query("SELECT d.*, p.company, p.contact_name, p.email "
                    "FROM drafts d JOIN prospects p ON p.id = d.prospect_id "
                    "WHERE d.verdict IS NULL ORDER BY d.id")


def drafts_for(db: Database, pid: str) -> list[sqlite3.Row]:
    return db.query("SELECT * FROM drafts WHERE prospect_id=? ORDER BY id", (pid,))


def has_pending_draft(db: Database, pid: str) -> bool:
    return db.one("SELECT 1 FROM drafts WHERE prospect_id=? AND verdict IS NULL",
                  (pid,)) is not None


def set_tg_message(db: Database, draft_id: int, message_id: int | None) -> None:
    db.execute("UPDATE drafts SET tg_message_id=? WHERE id=?", (message_id, draft_id))


def record_correction(db: Database, draft_id: int, subject: str, body: str,
                      instruction: str = "") -> None:
    """A human rewrote the draft. Keep the before/after pair: those pairs are the
    few-shot set the drafter learns from on the next run."""
    row = get_draft(db, draft_id)
    if row is None:
        raise KeyError(draft_id)
    db.execute(
        "INSERT INTO corrections (draft_id, prospect_id, kind, before_subject, "
        "before_body, after_subject, after_body, instruction, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (draft_id, row["prospect_id"], row["kind"], row["subject"], row["body"],
         subject, body, instruction, utcnow()))
    db.execute("UPDATE drafts SET subject=?, body=?, verdict='edited' WHERE id=?",
               (subject, body, draft_id))


def record_redo(db: Database, old_draft_id: int, new_draft: Draft,
                instruction: str) -> None:
    """[Redo] with an instruction is also a correction pair worth learning from:
    the old draft, what the human asked for, and what came out."""
    row = get_draft(db, old_draft_id)
    if row is None:
        raise KeyError(old_draft_id)
    db.execute(
        "INSERT INTO corrections (draft_id, prospect_id, kind, before_subject, "
        "before_body, after_subject, after_body, instruction, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (old_draft_id, row["prospect_id"], row["kind"], row["subject"], row["body"],
         new_draft.subject, new_draft.body, instruction, utcnow()))


def recent_corrections(db: Database, limit: int = 3) -> list[dict]:
    rows = db.query(
        "SELECT kind, before_subject, before_body, after_subject, after_body, "
        "instruction FROM corrections ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in reversed(rows)]


def mark_sent(db: Database, draft_id: int, today: date,
              message_id: str | None = None, verdict: str = "approved",
              channel: str = "", gmail_thread_id: str | None = None) -> dict:
    """Record a send and advance the ladder. Only path that increments touches."""
    row = get_draft(db, draft_id)
    if row is None:
        raise KeyError(draft_id)
    if row["sent_at"]:
        return {"already_sent": True, "prospect_id": row["prospect_id"]}
    pid = row["prospect_id"]
    prospect = get(db, pid)
    if prospect is None:
        raise KeyError(pid)

    with db.transaction():
        if row["verdict"] not in ("approved", "edited"):
            db.execute("UPDATE drafts SET verdict=? WHERE id=?", (verdict, draft_id))
        db.execute("UPDATE drafts SET sent_at=?, message_id=?, channel=? WHERE id=?",
                   (utcnow(), message_id, channel, draft_id))
        if row["kind"] in models.COLD_KINDS:
            touches = prospect.touches + 1
            first_touch = prospect.first_touch or today.isoformat()
            next_touch, next_kind = ladder.schedule_after_send(
                first_touch, touches, today)
            db.execute(
                "UPDATE prospects SET touches=?, first_touch=?, last_touch=?, "
                "next_touch=?, thread_id=COALESCE(thread_id, ?), "
                "gmail_thread_id=COALESCE(gmail_thread_id, ?) WHERE id=?",
                (touches, first_touch, today.isoformat(), next_touch,
                 message_id, gmail_thread_id, pid))
        else:                                  # a reply to their question
            touches, next_touch, next_kind = prospect.touches, prospect.next_touch, None
            db.execute("UPDATE prospects SET last_touch=?, thread_id=COALESCE(thread_id, ?), "
                       "gmail_thread_id=COALESCE(gmail_thread_id, ?) WHERE id=?",
                       (today.isoformat(), message_id, gmail_thread_id, pid))
        if prospect.status not in CLOSED:
            # The mail has already left by the time we get here; an unexpected
            # status must not roll back the record of the send.
            try:
                check_transition(prospect.status, "sent")
            except IllegalTransition:
                pass
            else:
                db.execute("UPDATE prospects SET status='sent' WHERE id=?", (pid,))
    return {"touches": touches, "next_touch": next_touch, "next_kind": next_kind,
            "prospect_id": pid}


def skip_draft(db: Database, draft_id: int, today: date) -> dict:
    row = get_draft(db, draft_id)
    if row is None:
        raise KeyError(draft_id)
    pid = row["prospect_id"]
    prospect = get(db, pid)
    db.execute("UPDATE drafts SET verdict='skipped' WHERE id=?", (draft_id,))
    if prospect and prospect.status == "queued":
        set_status(db, pid, "sent" if prospect.touches else "researched")
    when = ladder.push(ladder.SKIP_PUSH_DAYS, today)
    set_next_touch(db, pid, when)
    return {"prospect_id": pid, "next_touch": when}


def reject_draft(db: Database, draft_id: int, today: date) -> dict:
    out = skip_draft(db, draft_id, today)
    db.execute("UPDATE drafts SET verdict='rejected' WHERE id=?", (draft_id,))
    return out


def supersede_draft(db: Database, old_id: int, new_id: int | None = None) -> None:
    """[Redo]: the old card is retired; the new draft (if any) points back at it."""
    db.execute("UPDATE drafts SET verdict='superseded' WHERE id=? AND verdict IS NULL",
               (old_id,))
    if new_id is not None:
        db.execute("UPDATE drafts SET supersedes=? WHERE id=?", (old_id, new_id))


def cancel_pending(db: Database, pid: str, reason: str = "prospect closed") -> int:
    """Drop drafts still waiting for a verdict, and approved emails still
    waiting in the outbox. Called when a reply closes the prospect --
    otherwise a stale card sits in Telegram waiting to be tapped, or a
    "just following up" leaves an hour after they said no."""
    rows = db.query("SELECT id FROM drafts WHERE prospect_id=? AND verdict IS NULL",
                    (pid,))
    for row in rows:
        db.execute("UPDATE drafts SET verdict='skipped' WHERE id=?", (row["id"],))
        db.log_action(None, "cancel_draft", pid,
                      {"draft": row["id"], "reason": reason})
    return len(rows) + cancel_queued(db, pid, reason)


def cancel_queued(db: Database, pid: str, reason: str) -> int:
    """Outbox rows for this prospect that have not started sending."""
    rows = db.query("SELECT id, draft_id FROM outbox WHERE prospect_id=? "
                    "AND state='queued'", (pid,))
    for row in rows:
        db.execute("UPDATE outbox SET state='cancelled', reason=?, updated_at=? "
                   "WHERE id=? AND state='queued'", (reason, utcnow(), row["id"]))
        db.log_action(None, "cancel_send", pid,
                      {"draft": row["draft_id"], "reason": reason})
    return len(rows)


def has_queued_send(db: Database, pid: str) -> bool:
    return db.one("SELECT 1 FROM outbox WHERE prospect_id=? AND state IN "
                  "('queued','sending')", (pid,)) is not None


def queued_send(db: Database, draft_id: int) -> sqlite3.Row | None:
    return db.one("SELECT * FROM outbox WHERE draft_id=?", (draft_id,))


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------

def record_reply(db: Database, event, uid: str, action: str) -> int:
    cur = db.execute(
        "INSERT INTO replies (prospect_id, uid, message_id, from_addr, subject, "
        "snippet, label, confidence, extracted_ask, received_at, handled_at, action) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (event.prospect_id, uid, event.message_id, event.from_addr, event.subject,
         event.snippet[:500], event.label, event.confidence, event.extracted_ask,
         utcnow(), utcnow(), action))
    return int(cur.lastrowid)


def seen_message(db: Database, message_id: str) -> bool:
    if not message_id:
        return False
    return db.one("SELECT 1 FROM replies WHERE message_id=?", (message_id,)) is not None


def match_thread(db: Database, refs: Iterable[str], from_addr: str,
                 gmail_thread_id: str | None = None) -> str | None:
    """Which prospect does an inbound message belong to? Threading headers
    first, then the Gmail thread, then the sender address."""
    for ref in refs:
        row = db.one("SELECT prospect_id FROM drafts WHERE message_id=?", (ref,))
        if row:
            return row["prospect_id"]
        row = db.one("SELECT id FROM prospects WHERE thread_id=?", (ref,))
        if row:
            return row["id"]
    if gmail_thread_id:
        row = db.one("SELECT id FROM prospects WHERE gmail_thread_id=?",
                     (gmail_thread_id,))
        if row:
            return row["id"]
    if from_addr:
        row = db.one("SELECT id FROM prospects WHERE lower(trim(email))=?",
                     (from_addr.strip().lower(),))
        if row:
            return row["id"]
    return None


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def outreach_log(db: Database) -> list:
    """Every prospect we have actually written to, newest first.

    The audit trail already exists across drafts, replies and actions; this is
    the one query that turns it into the question a human asks -- "who have we
    mailed, and what happened?"
    """
    return db.query(
        """
        SELECT p.id, p.company, p.email, p.status, p.touches, p.next_touch,
               d.kind, d.subject, d.sent_at, d.channel, d.verdict,
               (SELECT label FROM replies r WHERE r.prospect_id = p.id
                ORDER BY r.id DESC LIMIT 1) AS reply
        FROM drafts d JOIN prospects p ON p.id = d.prospect_id
        WHERE d.sent_at IS NOT NULL
        ORDER BY d.sent_at DESC, d.id DESC
        """)


def stats(db: Database) -> dict:
    counts = {row["status"]: row["n"] for row in db.query(
        "SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
    drafts = {(row["verdict"] or "pending"): row["n"] for row in db.query(
        "SELECT verdict, COUNT(*) n FROM drafts GROUP BY verdict")}
    sent = db.one("SELECT COUNT(*) n FROM drafts WHERE sent_at IS NOT NULL")
    outbox = {row["state"]: row["n"] for row in db.query(
        "SELECT state, COUNT(*) n FROM outbox GROUP BY state")}
    return {"prospects": counts, "drafts": drafts, "sent": sent["n"] if sent else 0,
            "outbox": outbox,
            "blocked": (db.one("SELECT COUNT(*) n FROM do_not_contact")
                        or {"n": 0})["n"],
            "awaiting_contact": awaiting_contact(db),
            "total": sum(counts.values())}
