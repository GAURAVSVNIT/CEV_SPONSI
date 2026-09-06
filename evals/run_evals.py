#!/usr/bin/env python
"""Eval harness. Without numbers this is a demo, so: numbers.

    python evals/run_evals.py                    # offline, fake model, free
    python evals/run_evals.py --live             # the real model on frozen sources
    python evals/run_evals.py --inject-failure   # kill the first fetch of each case
    python evals/run_evals.py --json             # machine-readable only
    python evals/run_evals.py --cases DIR        # another fixture folder

Sources are frozen fixtures (data/fixtures/*.json), so a rerun measures the
pipeline and not the weather on someone's marketing site. The model is the only
thing that varies between --live runs.

What one case exercises, in order: research (the agent loop against the frozen
excerpts) -> draft (closed world, in the sender's voice) -> humanize (lint, one
rewrite pass when it fails) -> verify (checklist + per-claim grounding) -> one
regeneration with the checklist as feedback. That is the same path job.py
takes, minus the Telegram card.

Offline numbers measure the PIPELINE, not the model: the stub drafter is
deterministic by construction. Run --live before quoting a success rate.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sponsy import drafting, humanize, research, store, verify   # noqa: E402
from sponsy.config import DATA, Config, Event, Voice              # noqa: E402
from sponsy.llm import build_llm                                  # noqa: E402
from sponsy.models import Excerpt                                 # noqa: E402
from sponsy.state import Database                                 # noqa: E402
from sponsy.tools import ToolError                                # noqa: E402

CASES = DATA / "fixtures"
RESULTS = ROOT / "evals" / "results"
CRITERIA = ("specificity", "groundedness", "length", "ask_clarity", "tone", "human")

GREETING = re.compile(r"^\s*(hi|hello|dear|hey)\b[^\n]*\n+", re.I)
# Words that appear in every sponsorship email and so prove nothing about
# whether the opening line is about THIS company.
GENERIC = {"sponsorship", "sponsor", "sponsors", "competition", "event", "tier",
           "gold", "title", "associate", "alpha", "beta", "gamma", "delta",
           "participants", "prize", "brief", "call", "deck", "logo", "website",
           "certificates", "hackathon", "students", "student", "campus",
           "college", "colleges", "team", "quick", "running", "october",
           "february"}
STOPWORDS = {
    "about", "above", "after", "again", "against", "their", "there", "these",
    "those", "which", "while", "would", "could", "should", "with", "that",
    "this", "have", "has", "had", "for", "and", "the", "are", "was", "were",
    "they", "them", "from", "into", "over", "also", "been", "being", "your",
    "you", "our", "its", "it", "of", "in", "on", "to", "as", "at", "by", "is",
    "an", "a", "or", "be", "we", "us", "who", "how", "why", "what", "when",
    "company", "companies", "because", "since", "than", "then", "just",
}
TOKEN = re.compile(r"[a-z0-9][a-z0-9'&.-]*")


def tokens(text: str) -> set[str]:
    """Content words, lowercased, edge punctuation stripped. Kept local rather
    than imported from sponsy.verify so the scorer does not depend on a
    private helper of the module it is measuring."""
    out = set()
    for raw in TOKEN.findall(str(text or "").lower()):
        token = raw.strip(".-'&")
        if len(token) > 3 and token not in STOPWORDS:
            out.add(token)
    return out


def opening_sentence(body: str) -> str:
    body = GREETING.sub("", body or "", count=1).strip()
    parts = re.split(r"(?<=[.!?])\s", body)
    return parts[0] if parts else body


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_case(case: dict, draft, detail: dict, research_summary, voice) -> dict:
    """The rubric. Every criterion is a boolean so the aggregate is a rate."""
    sources_text = " ".join(s["text"] for s in case["sources"])
    source_tokens = tokens(sources_text)
    opening = opening_sentence(draft.body)
    specific_tokens = (tokens(opening) & source_tokens) - GENERIC
    wanted = set(case.get("expect", {}).get("specificity_tokens") or [])
    checks = detail["checks"]

    def wanted_hit() -> bool:
        # prefix match, so "api" in the fixture matches "apis" in the draft
        return any(w in t or t in w for w in wanted for t in specific_tokens)

    lint = humanize.lint(draft.body, draft.subject, voice)
    scores = {
        # the first sentence after the greeting is about them, in their words
        "specificity": bool(specific_tokens) and (not wanted or wanted_hit()),
        "groundedness": bool(checks.get("claims_traceable")) and not detail["ungrounded"],
        "length": bool(checks.get("length")),
        # one ask, one number (a tier amount): a pitch without a number is a hello
        "ask_clarity": bool(checks.get("has_ask")) and bool(re.search(r"\d", draft.body)),
        "tone": bool(checks.get("no_filler")) and bool(checks.get("no_superlatives")),
        # the deterministic AI-tell lint; same threshold the verifier uses
        "human": lint.score >= humanize.PASS_SCORE,
    }
    expect = case.get("expect", {})
    if expect.get("thin"):
        # the case that proves the rule: a thin company must produce a cautious
        # draft, not a confident invented one
        scores["thin_handling"] = (
            research_summary.confidence < 0.7
            and len(draft.claims) <= expect.get("max_claims", 1)
            and not detail["ungrounded"])
    return scores


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------

def _humanize(cfg, db, llm, draft, voice, counters: dict):
    """Mirror job._humanize: lint, and one model rewrite only when it fails."""
    if not cfg.humanize:
        return draft
    lint = humanize.lint(draft.body, draft.subject, voice)
    draft.ai_tell_score = lint.score
    if lint.score >= humanize.PASS_SCORE:
        return draft
    draft, cost = humanize.humanize_pass(llm, cfg, db, draft, voice, lint.issues,
                                         run_id=None)
    counters["cost"] += cost
    counters["steps"] += 1
    counters["humanized"] = True
    draft.humanized = True
    draft.ai_tell_score = humanize.lint(draft.body, draft.subject, voice).score
    return draft


def run_case(cfg, db, llm, event, voice, case: dict, inject_failure: bool) -> dict:
    pid = case["prospect_id"]
    db.execute("INSERT OR REPLACE INTO prospects (id, company, contact_name, email,"
               " segment, status, notes, source, created_at) VALUES (?,?,?,?,?,"
               "'new',?,'fixture',datetime('now'))",
               (pid, case["company"], case.get("contact_name", ""),
                case.get("email", ""), case.get("segment", ""),
                case.get("notes", "")))
    prospect = store.get(db, pid)
    frozen = [Excerpt(url=s["url"], title=s.get("title", ""), text=s["text"])
              for s in case["sources"]]
    counters = {"cost": 0.0, "steps": 0, "humanized": False}

    original = research.ResearchTools.fetch_page
    if inject_failure:
        # The first fetch of every case dies the way a real one does: with a
        # ToolError the registry turns into a value the model can read. What
        # we measure is whether the loop recovers and still produces a
        # verified draft from what it managed to get.
        state = {"failed": False}

        def flaky(self, url):
            if not state["failed"]:
                state["failed"] = True
                raise ToolError("injected failure: connection reset")
            return original(self, url)
        research.ResearchTools.fetch_page = flaky

    try:
        summary, excerpts, run = research.research_prospect(
            llm, cfg, db, prospect, event, run_id=None, frozen=frozen)
        draft, cost = drafting.draft_email(
            llm, cfg, db, prospect, summary, excerpts, event, voice, "initial")
        counters["cost"] += cost
        counters["steps"] += 1
        draft = _humanize(cfg, db, llm, draft, voice, counters)
        ok, detail = verify.verify_draft(
            draft, excerpts, event, cfg=cfg, llm=llm, company=prospect.company,
            db=db, run_id=None, contact_name=prospect.contact_name, voice=voice)
        regenerated = False
        if not ok:
            # exactly what job.py does: one more attempt with the checklist as
            # feedback, then it is queued flagged rather than dropped
            regenerated = True
            draft, cost = drafting.draft_email(
                llm, cfg, db, prospect, summary, excerpts, event, voice, "initial",
                feedback=verify.failure_feedback(detail))
            counters["cost"] += cost
            counters["steps"] += 1
            draft = _humanize(cfg, db, llm, draft, voice, counters)
            ok, detail = verify.verify_draft(
                draft, excerpts, event, cfg=cfg, llm=llm, company=prospect.company,
                db=db, run_id=None, contact_name=prospect.contact_name, voice=voice)
    finally:
        research.ResearchTools.fetch_page = original

    scores = score_case(case, draft, detail, summary, voice)
    return {
        "prospect_id": pid, "company": case["company"],
        "scores": scores, "verified": bool(ok), "regenerated": regenerated,
        "humanized": counters["humanized"],
        "human_score": round(float(draft.ai_tell_score), 2),
        "words": detail["words"], "claims": len(draft.claims),
        "sources_used": detail["sources"],
        "research_confidence": round(float(summary.confidence), 2),
        "failed_checks": [k for k, v in detail["checks"].items() if not v],
        "steps": run.steps + counters["steps"],
        # research + drafting + humanize. Grounding calls inside verify_draft
        # are not returned by its contract, so a hybrid --live run costs a
        # little more than this number says.
        "cost_usd": round(run.cost_usd + counters["cost"], 5),
        "subject": draft.subject, "opening": opening_sentence(draft.body),
    }


# ---------------------------------------------------------------------------
# aggregate + report
# ---------------------------------------------------------------------------

def summarise(rows: list[dict], model: str, injected: bool) -> dict:
    n = max(len(rows), 1)
    per_criterion = {
        name: round(sum(1 for r in rows if r["scores"].get(name, True)) / n, 3)
        for name in CRITERIA}
    thin = [r for r in rows if "thin_handling" in r["scores"]]
    passed = [r for r in rows if all(r["scores"].values())]
    return {
        "agent": "sponsorship",
        "run_at": date.today().isoformat(),
        "model": model,
        "cases": len(rows),
        "task_success_rate": round(len(passed) / n, 3),
        "per_criterion": per_criterion,
        "thin_source_handling": (round(sum(1 for r in thin
                                           if r["scores"]["thin_handling"])
                                       / len(thin), 3) if thin else None),
        "human_rate": per_criterion["human"],
        "humanize_pass_rate": round(sum(1 for r in rows if r["humanized"]) / n, 3),
        "escalation_rate": 1.0,          # every send is queued for a human
        "false_auto_rate": 0.0,          # nothing can auto-send: no such path
        "regeneration_rate": round(sum(1 for r in rows if r["regenerated"]) / n, 3),
        "verified_rate": round(sum(1 for r in rows if r["verified"]) / n, 3),
        "median_cost_usd": (round(statistics.median([r["cost_usd"] for r in rows]), 5)
                            if rows else 0),
        "median_steps": statistics.median([r["steps"] for r in rows]) if rows else 0,
        "injected_failure": injected,
        "cases_detail": rows,
    }


def report(rows: list[dict], summary: dict, path: Path) -> None:
    print("\nsponsorship eval - {} cases - model {}{}".format(
        len(rows), summary["model"],
        " - FIRST FETCH INJECTED TO FAIL" if summary["injected_failure"] else ""))
    print("-" * 84)
    print("{:<20} {:>5} {:>6} {:>5} {:>5} {:>3} {:>3} {:>3}  {}".format(
        "company", "words", "claims", "conf", "human", "ver", "hum", "reg",
        "failed criteria"))
    for row in rows:
        failed = [k for k, v in row["scores"].items() if not v]
        print("{:<20} {:>5} {:>6} {:>5.2f} {:>5.2f} {:>3} {:>3} {:>3}  {}".format(
            row["company"][:20], row["words"], row["claims"],
            row["research_confidence"], row["human_score"],
            "y" if row["verified"] else "n",
            "y" if row["humanized"] else "-",
            "y" if row["regenerated"] else "-", ", ".join(failed) or "-"))
    print("-" * 84)
    print("task success rate      {:.0%}  (every criterion)".format(
        summary["task_success_rate"]))
    for name, value in summary["per_criterion"].items():
        print("  {:<20} {:.0%}".format(name, value))
    if summary["thin_source_handling"] is not None:
        print("  {:<20} {:.0%}".format("thin-source handling",
                                       summary["thin_source_handling"]))
    print("human rate             {:.0%}  (lint score >= {})".format(
        summary["human_rate"], humanize.PASS_SCORE))
    print("needed a rewrite pass  {:.0%}".format(summary["humanize_pass_rate"]))
    print("escalation rate        {:.0%}  (by design: every send needs a human)"
          .format(summary["escalation_rate"]))
    print("false-auto rate        {:.0%}  (no auto-send path exists)"
          .format(summary["false_auto_rate"]))
    print("regeneration rate      {:.0%}".format(summary["regeneration_rate"]))
    print("verified first or second try  {:.0%}".format(summary["verified_rate"]))
    print("median cost per case   ${:.4f}".format(summary["median_cost_usd"]))
    print("median steps per case  {}".format(summary["median_steps"]))
    print("\nwrote {}\n".format(path))


def load_event(cfg: Config) -> Event:
    """The real event.yml when there is one, else the shipped example. Same
    rule as the CLI, so the eval drafts against the same facts a run would."""
    if Path(cfg.event_path).exists():
        return Event.load(cfg.event_path)
    return Event.load(DATA / "event.example.yml")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="use the real model (needs a provider key in .env)")
    parser.add_argument("--inject-failure", action="store_true",
                        help="fail the first fetch of every case")
    parser.add_argument("--cases", default="", metavar="DIR",
                        help="fixture folder (default data/fixtures)")
    parser.add_argument("--json", action="store_true", help="machine-readable only")
    args = parser.parse_args(argv)

    cfg = Config.load(offline=not args.live, dry_run=True,
                      db_path=Path(tempfile.mkdtemp()) / "evals.db")
    if args.live and cfg.resolved_llm_provider() == "fake":
        raise SystemExit("--live needs GEMINI_API_KEY, OPENAI_API_KEY or "
                         "ANTHROPIC_API_KEY in the environment or .env")
    # Frozen sources only, whatever .env says: the model is the one variable.
    # Nothing here posts to Telegram or touches a mailbox either.
    cfg.search_provider = "none"
    cfg.mail_provider = "manual"
    cfg.replies_provider = "none"
    cfg.telegram_bot_token = ""
    cfg.min_interval_s = 0 if not args.live else cfg.min_interval_s

    db = Database(cfg.db_path)
    store.ensure_schema(db)
    llm = build_llm(cfg, db=db)
    model = "fake" if cfg.resolved_llm_provider() == "fake" else cfg.resolved_models()[0]
    event = load_event(cfg)
    voice = Voice.load(cfg.voice_path, event)

    folder = Path(args.cases) if args.cases else CASES
    paths = sorted(folder.glob("*.json"))
    if not paths:
        raise SystemExit("no fixture cases in {}".format(folder))
    rows = []
    try:
        for path in paths:
            case = json.loads(path.read_text(encoding="utf-8"))
            rows.append(run_case(cfg, db, llm, event, voice, case, args.inject_failure))
    finally:
        db.close()

    summary = summarise(rows, model, args.inject_failure)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "sponsorship-{}{}.json".format(
        summary["run_at"], "-injected" if args.inject_failure else "")
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    report(rows, summary, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
