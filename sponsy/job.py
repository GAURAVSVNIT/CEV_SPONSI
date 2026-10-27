"""The daily job.

  gather()  verdicts tapped since the last run -> replies -> cold sweep -> due
  act()     research (agent loop, capped fetches) -> draft (closed world)
            -> humanize (lint, one rewrite) -> verify -> queue for approval
  verify()  re-reads the database and checks the invariants held
  report()  one summary line

Nothing here sends anything. The only path to an outbound email is a human
tapping [Send] on a card this job posted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from . import approvals, approve, drafting, humanize, ladder, research, store, verify
from . import replies as replies_mod
from .models import Draft, Excerpt, Research
from .telegram import esc


@dataclass
class Result:
    ok: bool = True
    confidence: float = 1.0
    summary: str = ""
    actions: list = field(default_factory=list)
    escalate: bool = False
    reason: str = ""
    steps: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0

    @property
    def status(self) -> str:
        if not self.ok:
            return "failed"
        return "escalated" if self.escalate else "success"


# A closed world older than this is refetched rather than reused: what a
# company says about its student programme in January is not evidence in
# December.
RESEARCH_TTL_DAYS = 30


class SponsorshipJob:
    name = "sponsorship"
    escalation_threshold = 0.75

    def __init__(self, cfg, db, notifier, llm=None, today: date | None = None,
                 force: bool = False, event=None, voice=None, limit: int = 10,
                 frozen: dict[str, list[Excerpt]] | None = None,
                 poll_replies: bool = True):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.llm = llm
        self.today = today or date.today()
        self.force = force
        self.event = event
        self.voice = voice
        self.limit = limit
        self.frozen = frozen or {}
        self.poll_replies = poll_replies
        self.run_id: int | None = None
        self.cost = 0.0
        self.steps = 0
        store.ensure_schema(db)
        # [Redo] on a card calls back into this job to write a new draft.
        approve.register(self.event, self.voice, today, regenerate=self.regenerate)

    # -- driver ------------------------------------------------------------
    def trigger(self) -> bool:
        """At most one successful run per calendar day unless forced."""
        if self.force:
            return True
        return not self.db.ran_on(self.name, self.today.isoformat())

    def run(self) -> Result | None:
        if not self.trigger():
            print("[{}] already ran today; use --force to override".format(self.name))
            return None
        started = time.time()
        self.run_id = self.db.start_run(self.name)
        try:
            inputs = self.gather()
            result = self.act(inputs)
            result = self.verify(result)
            result.elapsed_s = time.time() - started
            self.report(result)
            self.db.finish_run(self.run_id, status=result.status,
                               summary=result.summary, steps=result.steps,
                               cost_usd=result.cost_usd)
            return result
        except Exception as exc:
            self.db.fail_run(self.run_id, "{}: {}".format(type(exc).__name__, exc))
            self.notifier.error(self.name, exc)
            raise

    # -- gather ------------------------------------------------------------
    def gather(self) -> dict:
        applied = approvals.drain(self.cfg, self.db, self.notifier)
        self._flush_outbox()
        events = []
        if self.poll_replies and self.cfg.resolved_replies_provider() != "none":
            try:
                events = replies_mod.poll(self.llm, self.cfg, self.db,
                                          today=self.today, run_id=self.run_id)
            except Exception as exc:                 # mail must never break outreach
                print("[sponsorship] reply poll failed: {}".format(exc))
                self.db.log_action(self.run_id, "replies_error", "",
                                   {"error": str(exc)[:400]})
        cold = store.sweep_cold(self.db, self.today)
        due = store.due_today(self.db, self.today, self.limit)
        parked = store.awaiting_contact(self.db)
        return {"verdicts_applied": applied, "replies": events, "cold": cold,
                "due": due, "parked": parked}

    # -- act ---------------------------------------------------------------
    def act(self, inputs: dict) -> Result:
        actions: list[dict] = []
        queued = flagged = 0

        for event in inputs["replies"]:
            if event.label == "needs_info":
                prospect = store.get(self.db, event.prospect_id)
                if prospect and not store.has_pending_draft(self.db, prospect.id):
                    action = self._draft_and_queue(prospect, "needs_info",
                                                   ask=event.extracted_ask)
                    if action:
                        actions.append(action)
                        queued += 1
                        flagged += int(not action["verified"])
            elif event.label == "bounced":
                self.notifier.send(
                    "<b>{}</b>: the email bounced. {}.\n<code>{}</code>\nAdd another "
                    "address (web page, /add, or find-contacts) to continue.".format(
                        esc(event.prospect_id), esc(event.action),
                        esc(event.snippet[:300])))
            elif event.label == "interested":
                self.notifier.send(
                    "<b>{}</b> replied and sounds interested. Ladder cancelled; "
                    "this one is yours.\n<pre>{}</pre>".format(
                        esc(event.prospect_id), esc(event.snippet[:600])))

        for prospect in inputs["due"]:
            # step_for resolves touch 1 to `returning` for a company already
            # on our past-sponsor list; every later touch is the same either way.
            step = ladder.step_for(self.event, prospect.company, prospect.touches)
            if step is None:
                continue
            if self._undeliverable(prospect):
                continue
            action = self._draft_and_queue(prospect, step.kind)
            if action:
                actions.append(action)
                queued += 1
                flagged += int(not action["verified"])

        summary = ("{} queued ({} flagged) | {} replies | {} cold | "
                   "{} verdicts applied").format(
            queued, flagged, len(inputs["replies"]), len(inputs["cold"]),
            inputs["verdicts_applied"])
        parked = inputs.get("parked") or []
        if parked:
            summary += " | {} waiting on a contact address".format(len(parked))
        confidences = [a["confidence"] for a in actions] or [1.0]
        return Result(
            ok=True,
            confidence=round(sum(confidences) / len(confidences), 2),
            summary=summary,
            actions=actions,
            # Every send is irreversible, so every queued draft is an escalation
            # by definition. That is the whole design, not a fallback.
            escalate=queued > 0,
            reason="{} draft(s) waiting for approval".format(queued) if queued else "",
            steps=self.steps,
            cost_usd=self.cost)

    # -- one prospect ------------------------------------------------------
    def _reusable(self, prospect, stored, excerpts, kind: str) -> bool:
        """Is the stored closed world still worth writing from?

        Research is the expensive half of a run. It is reused whenever we have
        real sources that are not stale -- including for a repeated `initial`
        touch, which is what a skipped draft comes back as three days later.
        The two exceptions: research with no sources behind it is not research,
        and a *thin* result is worth one more attempt when we are starting the
        prospect over, because the pages may have been a bad draw.
        """
        if stored is None or not excerpts:
            return False
        when = store.latest_research_at(self.db, prospect.id)
        if when:
            try:
                age = (self.today - date.fromisoformat(when[:10])).days
            except ValueError:
                age = 0
            # Only genuinely old research is refetched. A *negative* age means
            # the stamp is ahead of this job's date, which happens whenever
            # local time runs behind UTC -- any timezone west of Greenwich, or
            # a --today in the past. That research is newer than now, not
            # staler, and treating it as stale re-researched every prospect on
            # every run: the exact free-tier burn this TTL exists to prevent.
            if age > RESEARCH_TTL_DAYS:
                return False
        return not (stored.thin and kind in ladder.FIRST_KINDS)

    def _research_for(self, prospect, kind: str):
        """Research once per prospect, not once per touch. Follow-ups -- and
        re-runs of a skipped touch -- reuse the same closed world: the model is
        only writing new copy."""
        stored = store.latest_research(self.db, prospect.id)
        stored_excerpts = store.excerpts_for(self.db, prospect.id)
        if self._reusable(prospect, stored, stored_excerpts, kind):
            return stored, stored_excerpts
        if kind == "needs_info":
            return (stored or Research(prospect_id=prospect.id, confidence=0.4),
                    stored_excerpts)
        summary, excerpts, run = research.research_prospect(
            self.llm, self.cfg, self.db, prospect, self.event,
            run_id=self.run_id, frozen=self.frozen.get(prospect.id))
        self.cost += run.cost_usd
        self.steps += run.steps
        store.save_research(self.db, summary, self.run_id)
        return summary, excerpts

    def _write_and_verify(self, prospect, summary: Research, excerpts: list[Excerpt],
                          kind: str, previous: list[dict], instruction: str = "",
                          prev_draft: dict | None = None) -> tuple[Draft, dict]:
        """draft -> lint -> (humanize) -> verify -> one regeneration on failure."""
        draft, cost = drafting.draft_email(
            self.llm, self.cfg, self.db, prospect, summary, excerpts, self.event,
            self.voice, kind, run_id=self.run_id, previous=previous,
            instruction=instruction, prev_draft=prev_draft)
        self.cost += cost
        self.steps += 1
        draft = self._humanize(draft)

        ok, detail = verify.verify_draft(
            draft, excerpts, self.event, cfg=self.cfg, llm=self.llm,
            company=prospect.company, db=self.db, run_id=self.run_id,
            contact_name=prospect.contact_name, voice=self.voice)
        if not ok:
            # one regeneration with the checklist as feedback, then queue it
            # flagged rather than silently dropping it
            self.db.log_action(
                self.run_id, "verify_reject", prospect.id,
                {"kind": kind, "failed": [k for k, v in detail["checks"].items() if not v],
                 "ungrounded": detail["ungrounded"], "attempt": 1}, confidence=0.0)
            draft, cost = drafting.draft_email(
                self.llm, self.cfg, self.db, prospect, summary, excerpts, self.event,
                self.voice, kind, run_id=self.run_id, previous=previous,
                feedback=verify.failure_feedback(detail), instruction=instruction,
                prev_draft=prev_draft)
            self.cost += cost
            self.steps += 1
            draft = self._humanize(draft)
            ok, detail = verify.verify_draft(
                draft, excerpts, self.event, cfg=self.cfg, llm=self.llm,
                company=prospect.company, db=self.db, run_id=self.run_id,
                contact_name=prospect.contact_name, voice=self.voice)

        draft.verified = ok
        draft.checks = detail
        draft.confidence = verify.confidence_from(detail, summary.confidence)
        return draft, detail

    def _humanize(self, draft: Draft) -> Draft:
        """Deterministic lint first; one model rewrite only when it fails.

        The lint always runs -- it is free, and the score is printed on the
        card. HUMANIZE=0 means "do not spend a model call rewriting", not
        "tell me the draft reads fine when nobody looked".
        """
        lint = humanize.lint(draft.body, draft.subject, self.voice)
        draft.ai_tell_score = lint.score
        if not self.cfg.humanize or lint.score >= humanize.PASS_SCORE:
            return draft
        self.db.log_action(self.run_id, "humanize", draft.prospect_id,
                           {"score": lint.score, "issues": lint.issues})
        try:
            draft, cost = humanize.humanize_pass(self.llm, self.cfg, self.db, draft,
                                                 self.voice, lint.issues,
                                                 run_id=self.run_id)
        except Exception as exc:                    # the lint still reports it
            print("[sponsorship] humanize pass failed: {}".format(exc))
            return draft
        self.cost += cost
        self.steps += 1
        # humanize_pass sets `humanized` only when it accepted the rewrite; a
        # rejected one comes back untouched and must not be labelled as edited.
        draft.ai_tell_score = humanize.lint(draft.body, draft.subject,
                                            self.voice).score
        return draft

    def _flush_outbox(self) -> None:
        """Send whatever approved email is due before drafting more. A cron
        that only runs `run` still drains the outbox this way."""
        if not self.cfg.paced:
            return
        from . import outbox  # noqa: PLC0415
        try:
            outbox.dispatch(self.cfg, self.db, self.notifier, self.event, self.voice)
        except Exception as exc:                    # sending must not stop drafting
            print("[sponsorship] outbox: {}".format(exc))

    def _undeliverable(self, prospect) -> bool:
        """Before a model call is spent on it: can this domain receive mail?

        Only positive evidence counts (no such domain, a null MX, no MX and
        no address). A lookup that fails says nothing and the draft goes
        ahead. Offline runs never look.
        """
        if self.cfg.offline or not getattr(self.cfg, "check_mx", True):
            return False
        from .mail import dns  # noqa: PLC0415
        domain = dns.domain_of(prospect.email)
        status, why = dns.cached_mail_domain_status(self.db, domain)
        if status != "none":
            return False
        reason = "bounce-risk: {} takes no mail ({})".format(domain, why)
        store.block_email(self.db, prospect.email, reason)
        self.db.log_action(self.run_id, "undeliverable", prospect.id,
                           {"email": prospect.email, "why": why}, confidence=1.0)
        self.notifier.send(
            "Not drafting for <b>{}</b> ({}): {} cannot receive email ({}). "
            "Find another address and it comes back.".format(
                esc(prospect.company), esc(prospect.id), esc(prospect.email), esc(why)))
        return True

    @staticmethod
    def _thread_subject(subject: str) -> str:
        """'Re: <the first email's subject>', with any Re: already on it
        removed so a long ladder does not read 'Re: Re: Re:'."""
        core = subject or ""
        while core[:3].lower() == "re:":
            core = core[3:].lstrip()
        return "Re: " + core

    def _post_card(self, prospect, draft: Draft, detail: dict) -> int:
        # What [Send] will attach, decided now so the card can say so before
        # you tap rather than after.
        from .mail import message as message_mod  # noqa: PLC0415

        if draft.kind in ("nudge", "new_angle", "last_touch") and \
                getattr(self.cfg, "followups_in_thread", True):
            # The follow-up goes out as a reply in the first email's thread
            # (outbox.threading_for sets In-Reply-To), so it carries that
            # thread's subject: Gmail only files a reply into a thread whose
            # subject matches, and "Re:" on a real reply is just the truth.
            first = self.db.one(
                "SELECT subject FROM drafts WHERE prospect_id=? AND sent_at IS NOT NULL "
                "AND message_id IS NOT NULL ORDER BY sent_at, id LIMIT 1", (prospect.id,))
            if first and (first["subject"] or "").strip():
                draft.subject = self._thread_subject(first["subject"])

        attachment = message_mod.attachment_for(self.cfg, draft.kind)
        if attachment is not None and not message_mod.attachment_problem(attachment):
            detail = dict(detail, attachment=attachment.name)
            draft.checks = detail
        action_id = self.db.log_action(
            self.run_id, "draft_email", prospect.id,
            {"kind": draft.kind, "subject": draft.subject, "body": draft.body,
             "claims": draft.claims, "verified": draft.verified,
             "humanized": draft.humanized, "ai_tell_score": draft.ai_tell_score,
             "failed": [k for k, v in detail["checks"].items() if not v]},
            confidence=draft.confidence, escalated=True)
        draft_id = store.insert_draft(self.db, draft, self.run_id, action_id)
        if prospect.status in ("new", "researched", "sent"):
            store.set_status(self.db, prospect.id, "queued")
        row = store.get_draft(self.db, draft_id)
        message_id = self.notifier.send(approve.draft_message(row, detail),
                                        buttons=approve.buttons(draft_id))
        store.set_tg_message(self.db, draft_id, message_id)
        return draft_id

    def _draft_and_queue(self, prospect, kind: str, ask: str | None = None) -> dict | None:
        summary, excerpts = self._research_for(prospect, kind)
        if not excerpts and kind != "needs_info":
            self.db.log_action(self.run_id, "research_empty", prospect.id,
                               {"company": prospect.company}, confidence=0.0,
                               escalated=True)
            store.set_next_touch(self.db, prospect.id,
                                 ladder.push(ladder.NO_SOURCES_PUSH_DAYS, self.today))
            self.notifier.send(
                "No sources found for <b>{}</b> ({}). Not drafting blind; will "
                "retry tomorrow.".format(esc(prospect.company), esc(prospect.id)))
            return None
        if kind in ladder.FIRST_KINDS and prospect.status == "new":
            store.set_status(self.db, prospect.id, "researched")

        if ask:
            summary.raw = dict(summary.raw or {})
            summary.raw["their_question"] = ask
            summary.student_relevance = (summary.student_relevance
                                         + "\nThey asked: " + ask).strip()
        previous = drafting.previous_touches(self.db, prospect.id)
        draft, detail = self._write_and_verify(prospect, summary, excerpts, kind,
                                               previous)
        draft_id = self._post_card(prospect, draft, detail)
        return {"draft_id": draft_id, "prospect_id": prospect.id, "kind": kind,
                "verified": draft.verified, "confidence": draft.confidence,
                "humanized": draft.humanized, "ai_tell_score": draft.ai_tell_score,
                "words": detail["words"], "sources": detail["sources"],
                "failed": [k for k, v in detail["checks"].items() if not v]}

    # -- [Redo] from Telegram ----------------------------------------------
    def regenerate(self, draft_row, instruction: str) -> int | None:
        """Write a new draft for the same prospect following the sender's
        instruction. The old card is superseded; a new card is posted."""
        prospect = store.get(self.db, draft_row["prospect_id"])
        if prospect is None:
            return None
        summary = (store.latest_research(self.db, prospect.id)
                   or Research(prospect_id=prospect.id, confidence=0.4))
        excerpts = store.excerpts_for(self.db, prospect.id)
        previous = drafting.previous_touches(self.db, prospect.id)
        draft, detail = self._write_and_verify(
            prospect, summary, excerpts, draft_row["kind"], previous,
            instruction=instruction,
            prev_draft={"subject": draft_row["subject"], "body": draft_row["body"]})
        draft.supersedes = int(draft_row["id"])
        store.supersede_draft(self.db, int(draft_row["id"]))
        store.record_redo(self.db, int(draft_row["id"]), draft, instruction)
        draft_id = self._post_card(prospect, draft, detail)
        self.db.log_action(None, "redo", prospect.id,
                           {"instruction": instruction, "old": int(draft_row["id"]),
                            "new": draft_id})
        return draft_id

    # -- verify ------------------------------------------------------------
    def verify(self, result: Result) -> Result:
        """Deterministic post-conditions, re-read from the database."""
        problems: list[str] = []
        for action in result.actions:
            row = store.get_draft(self.db, action["draft_id"])
            if row is None:
                problems.append("draft {} vanished".format(action["draft_id"]))
                continue
            if row["sent_at"]:
                problems.append("draft {} was sent without approval".format(row["id"]))
            prospect = store.get(self.db, row["prospect_id"])
            if prospect is None:
                problems.append("draft {} has no prospect".format(row["id"]))
                continue
            if prospect.status in ("declined", "cold", "interested"):
                problems.append("drafted for closed prospect " + prospect.id)
            if prospect.touches > ladder.MAX_TOUCHES:
                problems.append("touch cap breached for " + prospect.id)
            if store.is_blocked(self.db, prospect.email):
                problems.append("drafted to a blocked address: " + prospect.id)
        if problems:
            result.ok = False
            result.confidence = 0.0
            result.escalate = True
            result.reason = "; ".join(problems[:5])
        return result

    # -- report ------------------------------------------------------------
    def report(self, result: Result) -> None:
        n_actions = len(result.actions)
        flagged = [a for a in result.actions if not a["verified"]]
        head = "<b>Run done</b> | {} draft(s) | 0 sent automatically | {} need you".format(
            n_actions, n_actions)
        lines = [head, esc(result.summary)]
        for action in flagged:
            lines.append("! {} ({}) queued UNVERIFIED: {}".format(
                esc(action["prospect_id"]), esc(action["kind"]),
                esc(", ".join(action["failed"]))))
        if not result.ok:
            lines.append("VERIFIER FAILED: " + esc(result.reason))
        lines.append(self.notifier.run_footer(self.run_id or 0, result.steps,
                                              result.cost_usd, result.elapsed_s))
        self.notifier.send("\n".join(lines))
