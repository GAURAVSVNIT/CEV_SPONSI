"""Running for weeks on a server instead of minutes on a laptop.

  Stopper     SIGTERM (what `docker stop` and systemd send) becomes a clean
              exit -- but never in the middle of handing an email to the
              provider: the stop waits for that send to be recorded.
  heartbeat   a file touched on every loop, so a health check can tell a
              stuck process from a quiet one.
  Daily       the daily run at a fixed local time, from inside `serve`, so a
              server needs no cron.
  backup      a consistent copy of the SQLite file (the online backup API,
              safe while the other process writes), oldest copies pruned.
"""

from __future__ import annotations

import re
import signal
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .state import utcnow

JOB = "runtime"


class Stopper:
    def __init__(self):
        self.requested = False

    def install(self) -> "Stopper":
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):        # not the main thread, or Windows
                pass
        return self

    def _handle(self, signum, frame) -> None:
        from . import outbox  # noqa: PLC0415
        self.requested = True
        print("[runtime] stop requested{}".format(
            "; finishing the email in flight first" if outbox.sending() else ""))
        if not outbox.sending():
            raise KeyboardInterrupt

    def __call__(self) -> bool:
        return self.requested


def heartbeat_path(cfg) -> Path:
    return Path(cfg.db_path).parent / "heartbeat"


def heartbeat(cfg) -> None:
    path = heartbeat_path(cfg)
    try:
        path.write_text(utcnow() + "\n", encoding="utf-8")
    except OSError as exc:
        print("[runtime] heartbeat: {}".format(exc))


def heartbeat_age(cfg) -> float | None:
    """Seconds since the last heartbeat, or None if there never was one."""
    try:
        return max(0.0, time.time() - heartbeat_path(cfg).stat().st_mtime)
    except OSError:
        return None


class Daily:
    """Fires once per local day, at or after HH:MM. A failed run is not
    retried until tomorrow -- the notifier has already said why, and /run is
    one tap away -- because a run that fails on a dead API key would
    otherwise spend the whole day failing every 25 seconds."""

    def __init__(self, at: str):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", (at or "").strip())
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("--daily wants HH:MM (24-hour, local time), not {!r}"
                             .format(at))
        self.hour, self.minute = int(match.group(1)), int(match.group(2))

    def due(self, db, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if (now.hour, now.minute) < (self.hour, self.minute):
            return False
        return db.memory_get(JOB, "daily_attempt") != now.date().isoformat()

    def mark(self, db, now: datetime | None = None) -> None:
        db.memory_set(JOB, "daily_attempt", (now or datetime.now()).date().isoformat())


def backup(cfg, db, keep: int = 14, folder: Path | None = None) -> Path:
    """Copy the live database to data/backups/sponsy-YYYYmmdd-HHMMSS.db and
    keep the newest `keep` copies."""
    folder = Path(folder or Path(cfg.db_path).parent / "backups")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "sponsy-{}.db".format(datetime.now().strftime("%Y%m%d-%H%M%S"))
    dest = sqlite3.connect(str(target))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()
    copies = sorted(folder.glob("sponsy-*.db"))
    for old in copies[:-max(1, keep)]:
        try:
            old.unlink()
        except OSError:
            pass
    return target
