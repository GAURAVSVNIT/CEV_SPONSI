#!/usr/bin/env python
"""Command line.

    python run.py doctor                  check every secret and integration
    python run.py demo                    offline end-to-end run, no keys needed
    python run.py run [--limit N]         the daily run
    python run.py serve                   Telegram: tap buttons, use /commands
    python run.py serve --daily 09:30     ...and run the daily job itself (servers)
    python run.py web [--demo]            the same queue in a browser, on localhost
    python run.py pending                 drafts waiting for a verdict
    python run.py verdict d:3:send        approve from the terminal
    python run.py outbox [--wait]         approved emails waiting for their slot
    python run.py backup                  a consistent copy of the database
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from . import approvals, store
from .config import DATA, Config, Event, Voice
from .llm import LLMError, QuotaExhausted, build_llm
from .models import Excerpt
from .state import Database
from .telegram import build_notifier

FIXTURES = DATA / "fixtures"
DEMO_DB = DATA / "demo.db"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_fixtures(folder: Path) -> tuple[dict, list[dict]]:
    """Frozen fixtures: {prospect_id: [Excerpt]} plus the prospect rows."""
    frozen, rows = {}, []
    for path in sorted(Path(folder).glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        pid = case["prospect_id"]
        frozen[pid] = [Excerpt(url=s["url"], title=s.get("title", ""), text=s["text"])
                       for s in case["sources"]]
        rows.append(case)
    return frozen, rows


def seed_prospects(db: Database, rows: list[dict]) -> None:
    for case in rows:
        if store.get(db, case["prospect_id"]) is None:
            store.add_prospect(db, case["company"], email=case.get("email", ""),
                               contact_name=case.get("contact_name", ""),
                               segment=case.get("segment", ""),
                               notes=case.get("notes", ""), source="fixture",
                               pid=case["prospect_id"])


def load_event(cfg: Config) -> Event:
    if Path(cfg.event_path).exists():
        return Event.load(cfg.event_path)
    example = DATA / "event.example.yml"
    print("! {} is missing; using {} (copy it and edit it)".format(
        cfg.event_path, example))
    return Event.load(example)


def print_status(db: Database, cfg: Config) -> None:
    stats = store.stats(db)
    print("\nprospects ({})".format(stats["total"]))
    for status, count in sorted(stats["prospects"].items()):
        print("  {:<12} {}".format(status, count))
    print("drafts")
    for verdict, count in sorted(stats["drafts"].items()):
        print("  {:<12} {}".format(verdict, count))
    print("  {:<12} {}".format("sent", stats["sent"]))
    print("do-not-contact: {}".format(stats["blocked"]))
    parked = stats["awaiting_contact"]
    if parked:
        print("\nwaiting on a contact address ({}): {}".format(
            len(parked), ", ".join(parked[:12]) + (" ..." if len(parked) > 12 else "")))
        print("  find one:  python run.py find-contacts --apply")
    print("\nupcoming touches")
    for row in db.query("SELECT id, company, status, touches, next_touch FROM "
                        "prospects WHERE next_touch IS NOT NULL ORDER BY next_touch "
                        "LIMIT 15"):
        print("  {}  {:<22} {:<10} touch {}/4  -> {}".format(
            row["id"], row["company"][:22], row["status"], row["touches"],
            row["next_touch"]))
    print("\nlast runs")
    for row in db.query("SELECT id, status, steps, cost_usd, summary, started_at "
                        "FROM runs ORDER BY id DESC LIMIT 5"):
        print("  #{} {} {} | {} steps | ${:.4f} | {}".format(
            row["id"], (row["started_at"] or "")[:16], row["status"],
            row["steps"] or 0, row["cost_usd"] or 0, (row["summary"] or "")[:60]))
    print("\nchannels: llm={} search={} mail={} replies={} telegram={}".format(
        cfg.resolved_llm_provider(), cfg.resolved_search_provider(),
        cfg.resolved_mail_provider(), cfg.resolved_replies_provider(),
        "on" if cfg.telegram_enabled else "off"))
    print_outbox(db, cfg, brief=True)
    print()


def print_outbox(db: Database, cfg: Config, brief: bool = False) -> None:
    from . import outbox  # noqa: PLC0415
    info = outbox.status(cfg, db)
    print("\noutbox: {} today of {} allowed{} | {} queued | bounces this week: {} "
          "of {} sent".format(
              info["sent_today"], info["cap_today"],
              " (warm-up day {})".format(info["warmup_day"])
              if info["cap_today"] < info["daily_cap"] else "",
              len(info["queued"]), info["bounces_7d"], info["sends_7d"]))
    if info["paused"]:
        print("  PAUSED: {}{}".format(info["paused"]["reason"],
                                     " (until {})".format(info["paused"]["until"])
                                     if info["paused"]["until"] else
                                     " -- resume: python run.py outbox --resume"))
    if not info["paced"]:
        print("  (mail is {}; approvals are not paced)".format(
            cfg.resolved_mail_provider() if not cfg.dry_run else "a dry run"))
    print("  policy: " + info["policy"])
    if brief:
        return
    for row in info["queued"]:
        print("  #{} d:{} {:<22} {:<10} {}".format(
            row["id"], row["draft_id"], (row["company"] or "")[:22], row["kind"],
            outbox.describe_wait(cfg, row)))
    for row in info["recent"]:
        print("  #{} d:{} {:<22} {:<10} {}".format(
            row["id"], row["draft_id"], (row["company"] or "")[:22], row["state"],
            (row["reason"] or "")[:70]))


def print_pending(db: Database) -> None:
    rows = store.pending_drafts(db)
    if not rows:
        print("nothing waiting for a verdict.")
        return
    for row in rows:
        checks = json.loads(row["checks"] or "{}")
        failed = [k for k, v in (checks.get("checks") or {}).items() if not v]
        print("\n" + "=" * 70)
        print("draft {}  {}  ({})  kind={}  {}".format(
            row["id"], row["company"], row["prospect_id"], row["kind"],
            "VERIFIED" if row["verified"] else "UNVERIFIED: " + ", ".join(failed)))
        print("to: {}   human-score: {}".format(
            row["email"], row["ai_tell_score"] if row["ai_tell_score"] is not None
            else "n/a"))
        print("Subject: {}".format(row["subject"]))
        print()
        print(row["body"])
        print("\n  python run.py verdict d:{}:send   (or :skip, :never, "
              ":edit --text ..., :redo --text ...)".format(row["id"]))
    print()


def freeze_case(db: Database, pid: str) -> Path:
    """Write what we actually fetched for a prospect as a fixture."""
    prospect = store.get(db, pid)
    if prospect is None:
        raise SystemExit("no such prospect: " + pid)
    excerpts = store.excerpts_for(db, pid, limit=8)
    if not excerpts:
        raise SystemExit("no stored excerpts for {}; run the job first".format(pid))
    case = {
        "prospect_id": pid, "company": prospect.company,
        "contact_name": prospect.contact_name, "email": prospect.email,
        "segment": prospect.segment, "notes": prospect.notes,
        "synthetic": False,
        "note": "Frozen from a real run on {}.".format(date.today().isoformat()),
        "sources": [{"url": e.url, "title": e.title, "text": e.text} for e in excerpts],
        "expect": {"thin": False, "min_sources": len(excerpts),
                   "specificity_tokens": []},
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / "{}-{}.json".format(
        pid, prospect.company.lower().replace(" ", "-")[:24])
    path.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="path to the sqlite file")
    parser.add_argument("--date", help="pretend today is this date (YYYY-MM-DD)")
    parser.add_argument("--offline", action="store_true",
                        help="no network: fake model, no search, no mail, console output")
    parser.add_argument("--dry-run", action="store_true",
                        help="never send mail even if a provider is configured")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="check every secret and integration live")
    sub.add_parser("init-db", help="create the database")
    sub.add_parser("status", help="ladder state, run history, channels")
    sub.add_parser("pending", help="drafts waiting for a verdict")
    p = sub.add_parser("log", help="every email actually sent, and what came back")
    p.add_argument("--limit", type=int, default=50)
    sub.add_parser("drain", help="apply verdicts tapped in Telegram since the last pass")
    sub.add_parser("gmail-auth", help="run the Gmail OAuth flow once")
    p = sub.add_parser("test-mail", help="send one test email to yourself")
    p.add_argument("--to", default="", help="send it here instead of the "
                                            "address in event.yml")

    p = sub.add_parser("demo", help="offline end-to-end run on the frozen fixtures")
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("run", help="the daily run")
    p.add_argument("--limit", type=int, default=10, help="max prospects this run")
    p.add_argument("--force", action="store_true",
                   help="run even if it already ran today")
    p.add_argument("--no-replies", action="store_true", help="skip the reply poll")
    p.add_argument("--fixtures", metavar="DIR",
                   help="use frozen source fixtures instead of the web")

    p = sub.add_parser("serve", help="long-poll Telegram: buttons and /commands")
    p.add_argument("--limit", type=int, default=5, help="prospects per /run")
    p.add_argument("--daily", metavar="HH:MM", default="",
                   help="also run the daily job at this local time (default: "
                        "DAILY_RUN_AT in .env); a server then needs no cron")

    p = sub.add_parser("healthcheck", help="exit 0 if serve (or --url) is alive")
    p.add_argument("--url", default="", help="check this web server's /healthz instead")
    p.add_argument("--max-age", type=int, default=180,
                   help="seconds since serve's last heartbeat")

    p = sub.add_parser("outbox", help="approved emails waiting for their slot")
    p.add_argument("--send", action="store_true", help="send whatever is due now")
    p.add_argument("--wait", action="store_true",
                   help="keep sending, at the paced rate, until the queue is empty")
    p.add_argument("--pause", nargs="?", const="paused by hand", metavar="REASON",
                   help="stop sending until --resume")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--cancel", type=int, metavar="ID", help="drop one queued email")

    p = sub.add_parser("backup", help="copy the database to data/backups/")
    p.add_argument("--keep", type=int, default=14)

    p = sub.add_parser("web", help="browser UI on localhost: queue, prospects, agent")
    p.add_argument("--host", default="127.0.0.1",
                   help="anything but loopback needs a token")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--limit", type=int, default=5, help="prospects per run from the page")
    p.add_argument("--token", default="",
                   help="required on every API call (default: SPONSY_WEB_TOKEN)")
    p.add_argument("--demo", action="store_true",
                   help="offline, on {} with the frozen fixtures".format(DEMO_DB.name))
    p.add_argument("--open", action="store_true", help="open it in a browser")

    p = sub.add_parser("verdict", help="approve from the terminal")
    p.add_argument("data", metavar="d:ID:send|skip|never|edit|redo")
    p.add_argument("--text", default="", help="corrected email for :edit, or the "
                                              "instruction for :redo")

    p = sub.add_parser("discover", help="find companies that sponsor events like yours")
    p.add_argument("--query", action="append", help="extra search query (repeatable)")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--apply", action="store_true", help="add them to the list")

    p = sub.add_parser("find-contacts", help="find published addresses for prospects "
                                             "that have none")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--apply", action="store_true",
                   help="write the best candidate into the list")

    p = sub.add_parser("import", help="upsert prospects from a CSV")
    p.add_argument("csv")

    p = sub.add_parser("export", help="write prospects and their state to a CSV")
    p.add_argument("csv", nargs="?", default=str(DATA / "prospects.export.csv"))

    p = sub.add_parser("add", help="add one prospect")
    p.add_argument("company")
    p.add_argument("--email", default="")
    p.add_argument("--contact", default="", help="contact name")
    p.add_argument("--segment", default="")
    p.add_argument("--website", default="")
    p.add_argument("--notes", default="")

    p = sub.add_parser("simulate-reply", help="feed a reply in without mail")
    p.add_argument("prospect_id")
    p.add_argument("--subject", default="Re: sponsorship")
    p.add_argument("--text", required=True)

    p = sub.add_parser("freeze", help="write a prospect's fetched sources as a fixture")
    p.add_argument("prospect_id")
    return parser


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "healthcheck":
        # Docker runs this every minute: it must not load the model, the event
        # or anything else that can be slow or broken for unrelated reasons.
        return _healthcheck(args)

    demo = args.command == "demo" or getattr(args, "demo", False)
    offline = args.offline or demo
    db_path = args.db or (str(DEMO_DB) if demo else None)
    cfg = Config.load(offline=offline, dry_run=args.dry_run or None, db_path=db_path)
    today = date.fromisoformat(args.date) if args.date else date.today()
    db = Database(cfg.db_path)
    store.ensure_schema(db)
    notifier = build_notifier(cfg)
    llm = build_llm(cfg, db=db)
    event = load_event(cfg)
    voice = Voice.load(cfg.voice_path, event)

    from .job import SponsorshipJob  # noqa: PLC0415  (registers approval handlers)

    try:
        if args.command == "init-db":
            print("schema ready at {}".format(cfg.db_path))
            return 0

        if args.command == "doctor":
            from . import doctor  # noqa: PLC0415
            checks = doctor.run_checks(cfg, event=event, voice=voice, live=not offline)
            doctor.print_report(checks)
            return 0 if all(c.ok for c in checks if not c.optional) else 2

        if args.command == "status":
            print_status(db, cfg)
            return 0

        if args.command == "pending":
            print_pending(db)
            return 0

        if args.command == "log":
            rows = store.outreach_log(db)
            if not rows:
                print("nothing has been sent yet.")
                return 0
            print("\n{:<6} {:<24} {:<30} {:<11} {:<6} {:<10} {}".format(
                "id", "company", "to", "sent", "touch", "kind", "reply"))
            print("-" * 104)
            for row in rows[: args.limit]:
                print("{:<6} {:<24} {:<30} {:<11} {:<6} {:<10} {}".format(
                    row["id"], (row["company"] or "")[:24],
                    (row["email"] or "")[:30], (row["sent_at"] or "")[:10],
                    "{}/4".format(row["touches"]), row["kind"] or "",
                    row["reply"] or row["status"] or ""))
            print("\n{} email(s) sent. Full history: python run.py export\n"
                  .format(len(rows)))
            return 0

        if args.command == "import":
            report = store.import_csv(db, args.csv)
            print("imported: +{added} ~{updated} skipped {skipped}".format(**report))
            for problem in report["problems"]:
                print("  ! " + problem)
            return 0

        if args.command == "export":
            n = store.export_csv(db, args.csv)
            print("wrote {} prospect(s) to {}".format(n, args.csv))
            return 0

        if args.command == "add":
            existing = store.find_by_company(db, args.company)
            if existing:
                print("already on the list as {} ({})".format(existing.id,
                                                             existing.company))
                return 0
            pid = store.add_prospect(db, args.company, email=args.email,
                                     contact_name=args.contact, segment=args.segment,
                                     notes=args.notes, website=args.website,
                                     source="manual")
            print("added {} {}{}".format(pid, args.company,
                                        "" if args.email else " (no address yet)"))
            return 0

        if args.command == "gmail-auth":
            from .mail.gmail_api import GmailClient  # noqa: PLC0415
            client = GmailClient(cfg)
            client.authorize(interactive=True)
            print("authorised as {}".format(client.profile().get("emailAddress")))
            return 0

        if args.command == "test-mail":
            from . import mail  # noqa: PLC0415
            out = mail.test_send(cfg, event, voice, to=args.to)
            print(json.dumps(out, indent=2, default=str))
            return 0 if not out.get("error") else 2

        if args.command == "discover":
            from . import prospecting  # noqa: PLC0415
            print("reading sponsor pages of comparable events via {} ...\n".format(
                cfg.resolved_search_provider()))
            try:
                leads = prospecting.discover(cfg, llm, event, queries=args.query, db=db)
            except LLMError as exc:                # QuotaExhausted is one too
                print("stopped: {}".format(exc))
                return 2
            fresh = [x for x in leads if not prospecting.already_known(db, x.company)]
            print("{} companies found, {} not already on your list\n".format(
                len(leads), len(fresh)))
            for lead in fresh[: args.limit]:
                print("  {:<28} seen on {} sponsor page(s)  {}".format(
                    lead.company[:28], lead.strength, lead.website or ""))
                print("      {}".format(lead.sources[0]))
            if args.apply and fresh:
                for lead in fresh[: args.limit]:
                    pid = store.add_prospect(
                        db, lead.company, website=lead.website, source="discover",
                        notes="sponsors comparable events; seen on {} sponsor page(s), "
                              "e.g. {}".format(lead.strength, lead.sources[0]))
                    print("  added {} {}".format(pid, lead.company))
                print("\nadded with no address yet; run: python run.py find-contacts --apply")
            elif fresh:
                print("\nNothing was added. Re-run with --apply to add them.")
            return 0

        if args.command == "find-contacts":
            from . import contacts as contacts_mod  # noqa: PLC0415
            todo = [store.get(db, pid) for pid in store.awaiting_contact(db)]
            todo = [p for p in todo if p][: args.limit]
            if not todo:
                print("every prospect already has an address.")
                return 0
            print("searching for published addresses for {} prospect(s) via {}\n"
                  .format(len(todo), cfg.resolved_search_provider()))
            applied = 0
            for prospect in todo:
                finding = contacts_mod.find_for(cfg, prospect)
                print("{}  {}".format(prospect.id, prospect.company))
                if not finding.candidates:
                    if finding.form_urls:
                        print("    no public address -- apply through their page:")
                        for url in finding.form_urls[:2]:
                            print("        {}".format(url))
                    else:
                        print("    no public address and no sponsorship page found. "
                              "Try LinkedIn/X for their DevRel lead.")
                for cand in finding.candidates[:3]:
                    print("    {:<38} score {:>2}  {}".format(cand.email, cand.score,
                                                             cand.note))
                    print("        source: {}".format(cand.source_url))
                if args.apply and finding.best:
                    store.set_email(db, prospect.id, finding.best.email,
                                    source_url=finding.best.source_url)
                    applied += 1
                    print("    -> written to the prospect list")
                print()
            if not args.apply:
                print("Nothing was written. Re-run with --apply to accept the top "
                      "candidate for each, or: python run.py add ... --email")
            else:
                print("{} address(es) written.".format(applied))
            return 0

        if args.command == "freeze":
            print("wrote " + str(freeze_case(db, args.prospect_id)))
            return 0

        if args.command == "simulate-reply":
            from . import replies as replies_mod  # noqa: PLC0415
            prospect = store.get(db, args.prospect_id)
            if prospect is None:
                raise SystemExit("no such prospect")
            reply = replies_mod.classify_and_apply(
                llm, cfg, db, prospect, prospect.email, args.subject, args.text,
                message_id="<simulated-{}-{}>".format(prospect.id, today), today=today)
            print("{} -> {} ({:.2f}): {}".format(prospect.id, reply.label,
                                                 reply.confidence, reply.action))
            return 0

        # -- everything below needs a job (approval handlers + regenerate) --
        if args.command == "verdict":
            SponsorshipJob(cfg, db, notifier, llm=llm, today=today, force=True,
                           event=event, voice=voice, limit=5, poll_replies=False)
            said = approvals.apply_verdict(cfg, db, notifier, args.data, args.text)
            print(said)
            if said.startswith("approved; "):
                print("It is in the outbox. Something has to be running to send it: "
                      "`python run.py serve`, `web`, or `python run.py outbox --wait`.")
            return 0

        if args.command == "backup":
            from . import runtime  # noqa: PLC0415
            print("wrote " + str(runtime.backup(cfg, db, keep=args.keep)))
            return 0

        if args.command == "outbox":
            from . import outbox  # noqa: PLC0415
            # The handlers are needed for the card edits after a send.
            SponsorshipJob(cfg, db, notifier, llm=llm, today=None, force=True,
                           event=event, voice=voice, limit=5, poll_replies=False)
            if args.pause:
                outbox.pause(db, args.pause)
                print("sending paused: " + args.pause)
            if args.resume:
                outbox.resume(db)
                print("sending resumed")
            if args.cancel:
                print("cancelled" if outbox.cancel(db, args.cancel)
                      else "#{} is not waiting in the outbox".format(args.cancel))
            if args.send or args.wait:
                _drain_outbox(cfg, db, notifier, event, voice, wait=args.wait)
            print_outbox(db, cfg)
            return 0

        if args.command == "drain":
            SponsorshipJob(cfg, db, notifier, llm=llm, today=today, force=True,
                           event=event, voice=voice, limit=5, poll_replies=False)
            print("{} verdict(s) applied".format(approvals.drain(cfg, db, notifier)))
            return 0

        if args.command == "serve":
            from . import runtime  # noqa: PLC0415
            job = SponsorshipJob(cfg, db, notifier, llm=llm, today=None, force=True,
                                 event=event, voice=voice, limit=args.limit)
            _register_commands(cfg, db, notifier, llm, event, voice, args.limit)
            daily_at = args.daily or os.environ.get("DAILY_RUN_AT", "")
            daily = runtime.Daily(daily_at) if daily_at else None
            if daily:
                print("[serve] daily run at {:02d}:{:02d} local time".format(
                    daily.hour, daily.minute))
            stopper = runtime.Stopper().install()
            approvals.serve(cfg, db, notifier, should_stop=stopper,
                            tick=lambda: _serve_tick(cfg, db, notifier, llm, event,
                                                     voice, args.limit, daily))
            return 0

        if args.command == "web":
            from . import web  # noqa: PLC0415
            frozen: dict = {}
            if args.demo:
                frozen, rows = load_fixtures(FIXTURES)
                seed_prospects(db, rows)
            app = web.WebApp(cfg, event, voice, limit=args.limit, frozen=frozen,
                             token=args.token or os.environ.get("SPONSY_WEB_TOKEN", ""))
            web.serve(app, host=args.host, port=args.port, open_browser=args.open)
            return 0

        if args.command in ("run", "demo"):
            frozen: dict = {}
            fixtures_dir = getattr(args, "fixtures", None)
            if args.command == "demo" or fixtures_dir:
                folder = Path(fixtures_dir) if fixtures_dir else FIXTURES
                frozen, rows = load_fixtures(folder)
                seed_prospects(db, rows)
                print("[demo] {} frozen case(s) from {}".format(len(frozen), folder))
            elif not store.all_prospects(db):
                csv_path = DATA / "prospects.csv"
                if csv_path.exists():
                    report = store.import_csv(db, csv_path)
                    print("[run] imported {} prospect(s) from {}".format(
                        report["added"], csv_path))
            job = SponsorshipJob(
                cfg, db, notifier, llm=llm, today=today,
                force=getattr(args, "force", False) or args.command == "demo",
                event=event, voice=voice, limit=args.limit, frozen=frozen,
                poll_replies=not getattr(args, "no_replies", False))
            try:
                result = job.run()
            except LLMError as exc:
                # A quota cap, or a model outage that outlasted the retries (a
                # 503 "high demand"). The job has already marked the run failed
                # and told Telegram, and a failed run does not count as today's,
                # so the next run simply tries again. No traceback needed.
                print("stopped: {}".format(exc))
                return 2
            if result is None:
                return 0
            print("\n[{}] {} | confidence {:.2f} | {} steps | ${:.4f}".format(
                job.name, result.summary, result.confidence, result.steps,
                result.cost_usd))
            if args.command == "demo":
                print("\nNothing was sent. Look at what is queued:")
                print("  python run.py --db {} pending".format(cfg.db_path))
                print("  python run.py --db {} verdict d:1:send".format(cfg.db_path))
            return 0 if result.ok else 2

        parser.print_help()
        return 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# long-running loops
# ---------------------------------------------------------------------------

def _serve_tick(cfg, db, notifier, llm, event, voice, limit: int, daily) -> None:
    """Between two Telegram polls: heartbeat, outbox, and the daily run."""
    from . import outbox, runtime  # noqa: PLC0415
    from .job import SponsorshipJob  # noqa: PLC0415

    runtime.heartbeat(cfg)
    outbox.dispatch(cfg, db, notifier, event, voice)
    if daily is None or not daily.due(db):
        return
    daily.mark(db)
    try:
        print("[serve] backup: {}".format(runtime.backup(cfg, db)))
    except Exception as exc:                       # noqa: BLE001
        print("[serve] backup failed: {}".format(exc))
    try:
        result = SponsorshipJob(cfg, db, notifier, llm=llm, today=None, force=False,
                                event=event, voice=voice, limit=limit).run()
        print("[serve] daily run: {}".format(result.summary if result else "skipped"))
    except LLMError as exc:
        print("[serve] daily run stopped: {}".format(exc))
    finally:
        # The run's job registered its own handlers; re-register a fresh one
        # so [Redo] keeps calling back into a live job.
        SponsorshipJob(cfg, db, notifier, llm=llm, today=None, force=True,
                       event=event, voice=voice, limit=limit)


def _healthcheck(args) -> int:
    from . import runtime  # noqa: PLC0415
    if args.url:
        import urllib.request  # noqa: PLC0415
        try:
            with urllib.request.urlopen(args.url.rstrip("/") + "/healthz", timeout=8) as r:
                return 0 if r.status == 200 else 1
        except Exception as exc:                   # noqa: BLE001
            print("unhealthy: {}".format(exc))
            return 1
    cfg = Config.load(db_path=args.db)
    age = runtime.heartbeat_age(cfg)
    if age is None or age > args.max_age:
        print("unhealthy: last heartbeat {}".format(
            "never" if age is None else "{:.0f}s ago".format(age)))
        return 1
    print("ok: heartbeat {:.0f}s ago".format(age))
    return 0


def _drain_outbox(cfg, db, notifier, event, voice, wait: bool) -> None:
    """`outbox --send` (one pass) or `--wait` (until nothing is queued)."""
    from . import outbox  # noqa: PLC0415
    while True:
        for out in outbox.dispatch(cfg, db, notifier, event, voice):
            print("  #{} {}{}".format(out.get("id"), out.get("state"),
                                     ": " + str(out.get("reason")) if out.get("reason")
                                     else ""))
        if not wait:
            return
        hold = outbox.paused(db)
        nxt = db.one("SELECT MIN(not_before) t FROM outbox WHERE state='queued'")["t"]
        if hold and not hold["until"]:
            print("paused ({}); resume with --resume".format(hold["reason"]))
            return
        if not nxt:
            return
        due = outbox._parse(hold["until"] if hold else nxt)
        delay = max(5.0, min(300.0, (due - outbox._now()).total_seconds()))
        print("next send in about {:.0f}s; ctrl-c to stop waiting (the queue keeps)"
              .format(delay))
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return


# ---------------------------------------------------------------------------
# Telegram slash-commands (serve mode)
# ---------------------------------------------------------------------------

def _register_commands(cfg, db, notifier, llm, event, voice, limit: int) -> None:
    from .job import SponsorshipJob  # noqa: PLC0415

    def cmd_help(ctx, args: str) -> str:
        return ("/pending - drafts waiting for you\n"
                "/status - ladder state\n"
                "/run [N] - research and draft N prospects now\n"
                "/discover - find companies that sponsor events like ours\n"
                "/add Company | email | notes - add a prospect\n"
                "/never P01 - never contact this prospect again\n"
                "/outbox - approved emails waiting to go out\n"
                "/pause, /resume - stop or restart sending\n"
                "/cancel N - drop outbox email #N before it leaves\n"
                "/help - this")

    def cmd_pending(ctx, args: str) -> str:
        rows = store.pending_drafts(db)
        if not rows:
            return "nothing waiting for a verdict."
        return "\n".join("d:{} {} ({}) {}".format(r["id"], r["company"], r["kind"],
                                                  "ok" if r["verified"] else "UNVERIFIED")
                         for r in rows)

    def cmd_status(ctx, args: str) -> str:
        stats = store.stats(db)
        parts = ["{} {}".format(v, k) for k, v in sorted(stats["prospects"].items())]
        return "prospects: {}\nsent: {} | blocked: {} | waiting on address: {}".format(
            ", ".join(parts) or "none", stats["sent"], stats["blocked"],
            len(stats["awaiting_contact"]))

    def cmd_run(ctx, args: str) -> str:
        try:
            n = int(args.strip() or limit)
        except ValueError:
            n = limit
        job = SponsorshipJob(cfg, db, notifier, llm=llm, today=None, force=True,
                             event=event, voice=voice, limit=n)
        try:
            result = job.run()
        except QuotaExhausted as exc:
            return "stopped: {}".format(exc)
        return result.summary if result else "did not run"

    def cmd_discover(ctx, args: str) -> str:
        from . import prospecting  # noqa: PLC0415
        try:
            leads = prospecting.discover(cfg, llm, event, db=db)
        except QuotaExhausted as exc:
            return "stopped: {}".format(exc)
        fresh = [x for x in leads if not prospecting.already_known(db, x.company)]
        if not fresh:
            return "nothing new found."
        added = []
        for lead in fresh[:limit]:
            pid = store.add_prospect(db, lead.company, website=lead.website,
                                     source="discover",
                                     notes="seen on {} sponsor page(s), e.g. {}".format(
                                         lead.strength, lead.sources[0]))
            added.append("{} {}".format(pid, lead.company))
        return "added (no address yet):\n" + "\n".join(added)

    def cmd_add(ctx, args: str) -> str:
        parts = [p.strip() for p in args.split("|")]
        if not parts or not parts[0]:
            return "usage: /add Company | email | notes"
        company = parts[0]
        email = parts[1] if len(parts) > 1 else ""
        notes = parts[2] if len(parts) > 2 else ""
        existing = store.find_by_company(db, company)
        if existing:
            if email and not existing.email:
                store.set_email(db, existing.id, email, source_url="telegram")
                return "{} already listed as {}; address added.".format(company,
                                                                       existing.id)
            return "{} is already on the list as {}.".format(company, existing.id)
        pid = store.add_prospect(db, company, email=email, notes=notes,
                                 source="telegram")
        return "added {} {}{}".format(pid, company, "" if email else " (no address yet)")

    def cmd_never(ctx, args: str) -> str:
        pid = args.strip()
        if not pid or store.get(db, pid) is None:
            return "usage: /never P01"
        n = store.mark_never(db, pid)
        return "{} marked declined permanently; {} pending draft(s) cancelled.".format(
            pid, n)

    def cmd_outbox(ctx, args: str) -> str:
        from . import outbox  # noqa: PLC0415
        info = outbox.status(cfg, db)
        lines = ["{} sent today of {} allowed | bounces this week {} of {}".format(
            info["sent_today"], info["cap_today"], info["bounces_7d"], info["sends_7d"])]
        if info["paused"]:
            lines.append("PAUSED: {}".format(info["paused"]["reason"]))
        for row in info["queued"][:15]:
            lines.append("#{} {} ({}) {}".format(row["id"], row["company"], row["kind"],
                                                outbox.describe_wait(cfg, row)))
        if not info["queued"]:
            lines.append("nothing waiting.")
        return "\n".join(lines)

    def cmd_pause(ctx, args: str) -> str:
        from . import outbox  # noqa: PLC0415
        outbox.pause(db, args.strip() or "paused from Telegram")
        return "sending paused; /resume to restart"

    def cmd_resume(ctx, args: str) -> str:
        from . import outbox  # noqa: PLC0415
        outbox.resume(db)
        return "sending resumed"

    def cmd_cancel(ctx, args: str) -> str:
        from . import outbox  # noqa: PLC0415
        try:
            n = int(args.strip().lstrip("#"))
        except ValueError:
            return "usage: /cancel 12  (the # from /outbox)"
        return "cancelled #{}".format(n) if outbox.cancel(db, n) else \
            "#{} is not waiting in the outbox".format(n)

    for name, fn in (("help", cmd_help), ("start", cmd_help), ("pending", cmd_pending),
                     ("status", cmd_status), ("run", cmd_run),
                     ("discover", cmd_discover), ("add", cmd_add), ("never", cmd_never),
                     ("outbox", cmd_outbox), ("pause", cmd_pause),
                     ("resume", cmd_resume), ("cancel", cmd_cancel)):
        approvals.register_command(name, fn)


if __name__ == "__main__":
    raise SystemExit(main())
