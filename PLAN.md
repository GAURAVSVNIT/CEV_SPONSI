# SPONSY — sponsorship outreach agent: architecture & build plan

One agent, one job: find companies likely to sponsor the club's event, research
each one from pages it actually fetched, write an email that sounds like the
sender wrote it, post it to Telegram, and — only after one tap — send it through
Gmail. Then run the follow-up ladder and read replies.

**The invariant:** there is no code path that sends an email without a human
verdict recorded in the database first. SQLite triggers enforce it; the Python
just cooperates.

Ancestry: `E:\diff agents\ops-agent` (closed-world drafting, DB guardrails,
ladder, Telegram approvals) and `mailforge-ai` (Tavily research service,
pain-point/hook analysis, OpenAI-compatible model client). This is a rewrite,
not a fork: single job, three model providers, Gmail API delivery, a real
humanizer, Telegram slash-commands, and a `doctor` that checks every secret.

---

## 1. The daily loop

```
gather   drain Telegram verdicts tapped since last run
         poll replies (Gmail API or IMAP, read-only) -> classify -> transition
         cold sweep (day 21) -> who is due today
   |
research agent loop: web_search + fetch_page, hard cap 4 fetches/prospect
         every fetched page stored in `research` with its URL (the closed world)
   |
draft    model sees ONLY research + excerpts + our event facts + our voice
         must enumerate `claims`; writes in the sender's voice, plain text
   |
humanize deterministic AI-tell lint (banned phrases, em-dashes, uniform
         sentences, no contractions, listicle structure, salesy subject)
         -> if it fails, one rewrite pass constrained to keep claims/numbers
   |
verify   12-item checklist + per-claim groundedness against the excerpts
         fail -> one regeneration with the checklist as feedback
         fail again -> queue flagged UNVERIFIED (never silently dropped)
   |
report   Telegram card:  [Send] [Edit] [Redo] [Skip] [Never]
   |
tap      Send -> Gmail API (or SMTP app password) -> card edited to "Sent"
         -> ladder schedules the next touch (day 3 / 7 / 14; cold on 21)
```

## 2. Layout

```
run.py                     entry point -> sponsy.cli.main
sponsy/
  config.py                .env loader; Config, Event (event.yml), Voice (voice.yml)
  state.py                 SQLite wrapper; runs / actions / memory (the audit log)
  models.py                Prospect, Excerpt, Research, Draft, ReplyEvent; status machine
  store.py                 sponsorship tables, the six triggers, all queries
  ladder.py                touch timing (0/3/7/14, cold 21), 4-touch cap
  tools.py                 provider-neutral tool registry
  agent.py                 run_agent() tool loop + structured() one-shot JSON
  prompts.py               every prompt, one file; "TASK: x" first line for the fake
  llm/
    __init__.py            BaseLLM, LLMResponse, ToolCall, parse_json, build_llm, errors
    gemini.py              Gemini REST, key rotation, throttle, thinking budget
    openai_compat.py       /chat/completions (OpenAI, NVIDIA NIM, Groq, OpenRouter, Featherless)
    anthropic_.py          Anthropic SDK (Messages API, tool use, adaptive thinking)
    fake.py                FakeLLM + @register_fake handlers (offline/tests/evals)
  research.py              search providers, robots-aware fetch, excerpting, ResearchTools
  prospecting.py           discover companies from sponsor pages of comparable events
  contacts.py              find a published role mailbox, fail closed
  drafting.py              closed-world drafter with voice + few-shot corrections
  humanize.py              AI-tell lint + rewrite pass
  verify.py                checklist, grounded(), greeting check, confidence
  replies.py               classification + status transitions; Gmail/IMAP transports
  mail/
    __init__.py            deliver(): pick provider, always write .eml, mailto fallback
    message.py             EmailMessage construction, threading headers
    gmail_api.py           OAuth2 desktop flow, send-as-user, thread-aware, list replies
    smtp.py                Gmail SMTP with app password
  telegram.py              Bot API client (send/edit/answer/force_reply/get_updates), console fallback
  approvals.py             verdict routing, drain()/serve(), slash-commands, chat allowlist
  approve.py               card formatting, buttons, send/edit/redo/skip/never handlers
  job.py                   SponsorshipJob: gather -> act -> verify -> report
  doctor.py                live checks of every integration and secret
  cli.py                   subcommands
data/
  event.yml                the event (gitignored; has a phone number)  event.example.yml
  voice.yml                how the sender writes (gitignored)           voice.example.yml
  prospects.csv            the list
  fixtures/*.json          frozen sources for demo/tests/evals
  outbox/                  every approved email as .eml + notifications.log
  gmail_credentials.json   OAuth client (gitignored)   gmail_token.json (gitignored)
tests/                     offline; FakeLLM; no network
evals/run_evals.py         frozen-source eval harness
.github/workflows/daily.yml
```

## 3. Configuration (`.env`)

| Group | Keys |
|---|---|
| model | `LLM_PROVIDER=auto\|gemini\|openai\|anthropic\|fake`, `GEMINI_API_KEY`, `GEMINI_API_KEYS`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_MODEL_CHEAP`, `LLM_THINKING_BUDGET`, `LLM_MAX_STEPS`, `LLM_MAX_COST`, `LLM_MIN_INTERVAL` |
| telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| research | `SEARCH_PROVIDER=auto\|tavily\|brave\|ddg\|none`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `MAX_FETCHES` |
| quality | `GROUNDING=hybrid\|keyword\|model\|off`, `HUMANIZE=1`, `HUMANIZE_STRICT=0` |
| mail | `MAIL_PROVIDER=auto\|gmail_api\|smtp\|manual`, `GMAIL_CREDENTIALS`, `GMAIL_TOKEN`, `GMAIL_SENDER`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALLOW_SEND=1` |
| replies | `REPLIES_PROVIDER=auto\|gmail_api\|imap\|none`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` |
| runtime | `SPONSY_DB`, `SPONSY_EVENT`, `SPONSY_VOICE` |

`auto` resolution: llm = first of gemini/openai/anthropic with a key, else fake.
mail = gmail_api if a token or credentials file exists, else smtp if a password
is set, else manual. replies = gmail_api if mail is gmail_api, else imap if
configured, else none.

## 4. Module contracts (what each file exports)

Everything below is the interface `job.py`, `cli.py` and the tests are written
against. Implementations may add helpers but must provide these names with
these signatures.

### config.py
- `ROOT: Path`, `DATA: Path`, `load_dotenv(path=None)`
- `class Config` (dataclass; fields per §3 plus `db_path, outbox, event_path, voice_path, dry_run, offline, user_agent, fetch_timeout, max_fetches`); `Config.load(**overrides)`; properties `api_keys`, `telegram_enabled`, `imap_enabled`, `smtp_enabled`, `gmail_api_enabled`; methods `resolved_llm_provider()`, `resolved_search_provider()`, `resolved_mail_provider()`, `resolved_replies_provider()`, `resolved_models() -> (model, cheap)`, `price(model) -> (in, out)`
- `class Tier(name, amount, deliverables, currency)`, `class Event(...)` with `load(path)`, `tier_block()`, `track_record_block()`, `tier_names`
- `class Voice(sender_name, sender_role, club, greeting, signoff, tone_notes, phrases_i_use, phrases_i_avoid, sample_emails, max_words)` with `Voice.load(path, event) -> Voice` (falls back to event fields when the file is absent), `style_block() -> str`, `examples_block() -> str`

### state.py
- `utcnow()`, `class Database(path)`: `execute, executescript, query, one, transaction(), close, start_run, finish_run, fail_run, last_run, ran_on, log_action, set_verdict, memory_get, memory_set`

### models.py
- `STATUSES, TRANSITIONS, CLOSED, KINDS, COLD_KINDS, REPLY_LABELS, IllegalTransition, check_transition`
- `Prospect(id, company, contact_name, email, segment, status, last_touch, first_touch, next_touch, thread_id, gmail_thread_id, touches, notes, website, source)` + `from_row`
- `Excerpt(url, text, title)` + `block(i)`
- `Research(prospect_id, one_liner, student_relevance, prior_campus_sponsorship, recent_signal, hook, sources_used, confidence, raw)` + `thin`, `to_json()`
- `Draft(prospect_id, kind, subject, body, claims, suggested_tier, id, checks, verified, confidence, humanized, ai_tell_score, supersedes)` + `words`
- `ReplyEvent(prospect_id, message_id, from_addr, subject, snippet, label, confidence, extracted_ask, action)`

### store.py
- `ensure_schema(db)`; tables `prospects, research, research_summary, drafts, replies, corrections, do_not_contact`
- triggers: declined permanent; touch cap; no draft past cap; no draft when closed; no draft to do-not-contact; **no send without verdict** (`sent_at` requires verdict in approved/edited)
- prospects: `import_csv, export_csv, add_prospect(db, company, **fields) -> id, get, all_prospects, due_today, set_status, set_next_touch, set_email, sweep_cold, block_email, is_blocked, awaiting_contact, mark_never`
- research: `save_excerpt, excerpts_for, save_research, latest_research`
- drafts: `insert_draft, get_draft, pending_drafts, has_pending_draft, record_correction, recent_corrections(limit=3), mark_sent, skip_draft, reject_draft, supersede_draft, cancel_pending, set_tg_message, drafts_for`
- replies: `record_reply, seen_message, match_thread`
- `stats(db)`

### llm/
- `LLMResponse(text, tool_calls, prompt_tokens, output_tokens, cost_usd, finish_reason, raw, parts)`; `ToolCall(name, args, id, thought_signature)`
- message format between agent.py and clients: `{"role":"user","text"}`, `{"role":"model","text","tool_calls":[ToolCall],"parts":[provider raw]}`, `{"role":"tool","name","id","response":dict}`
- `BaseLLM.generate(*, system, messages, tools=None, response_json=False, temperature=0.3, model=None) -> LLMResponse`
- `build_llm(cfg, model=None, db=None)`; `LLMError`, `QuotaExhausted`, `parse_json`, `register_fake`, `FakeLLM`
- tool declarations are passed provider-neutral: `[{"name","description","parameters":<json schema>}]`; each client converts.

### agent.py
- `run_agent(llm, *, system, user, registry, max_steps, max_cost, db, run_id, target, temperature=0.2, raise_on_budget=True) -> AgentRun(text, cost_usd, steps, elapsed_s, tool_calls, messages, hit_budget)` with `.json()`
- `structured(llm, *, system, user, model=None, temperature=0.2, retries=1, db, run_id, kind, target) -> (dict, cost)`

### research.py
- `html_to_text(html) -> (title, text)`, `make_excerpt(text, limit)`, `robots_allows(cfg, url)`, `fetch_url(cfg, url) -> (title, text)`, `search(cfg, query, k=5) -> [{title,url,snippet}]`
- `class ResearchTools(cfg, db, prospect, run_id=None, frozen=None)` with `web_search`, `fetch_page`, `registry()`, `fetched`, `budget_left`
- `research_prospect(llm, cfg, db, prospect, event, run_id=None, frozen=None) -> (Research, [Excerpt], AgentRun)`

### prospecting.py
- `class Lead(company, events:set, sources:list, website:str)` + `strength`
- `default_queries(event) -> list[str]`; `discover(cfg, llm, event, queries=None, max_pages=6, db=None, run_id=None) -> [Lead]`; `already_known(db, company)`; `clean_name(name)`

### contacts.py
- `score_email(address, company, source_url) -> (score, why)`; `class Candidate(email, source_url, score, note)`; `class Finding(prospect_id, company, candidates, pages_seen, form_urls)` + `best`; `find_for(cfg, prospect, max_pages=3) -> Finding`

### drafting.py
- `draft_email(llm, cfg, db, prospect, research, excerpts, event, voice, kind, run_id=None, previous=None, feedback="", instruction="") -> (Draft, cost)`; `previous_touches(db, pid, limit=3)`; `fewshot_block(db, voice) -> str`

### humanize.py
- `BANNED_PHRASES: tuple`, `class LintResult(score: float, issues: list[str], flags: dict)`; `lint(body, subject="", voice=None) -> LintResult` (score 1.0 = clean; pass threshold 0.7)
- `humanize_pass(llm, cfg, db, draft, voice, issues, run_id=None) -> (Draft, cost)` — rewrites body/subject only; keeps `claims`; may not add a number absent from the original body

### verify.py
- `greeting_ok(body, contact_name) -> (ok, why)`; `grounded(claim, excerpts, cfg, llm, db, run_id, allowed_numbers, own_facts) -> (ok, why)`; `own_facts(event)`
- `verify_draft(draft, excerpts, event, cfg=None, llm=None, company="", db=None, run_id=None, contact_name="", voice=None) -> (ok, detail)`; `detail = {"checks": {...}, "claims": [...], "words", "ungrounded", "sources", "greeting", "lint": LintResult-as-dict}`; checks: `length, names_company, has_ask, no_placeholder, no_filler, subject_ok, claims_traceable, no_superlatives, greets_our_contact, has_claims, not_empty, sounds_human`
- `failure_feedback(detail) -> str`; `confidence_from(detail, research_confidence) -> float`

### mail/
- `message.build_message(cfg, event, voice, prospect, subject, body, in_reply_to=None) -> EmailMessage`; `message.mailto_url(prospect, subject, body)`; `message.write_eml(cfg, msg, prospect, kind, today) -> Path`
- `gmail_api.GmailClient(cfg)`: `authorize(interactive=True)`, `is_authorized`, `profile() -> {emailAddress}`, `send(msg, thread_id=None) -> {"message_id": <rfc header>, "gmail_id", "thread_id"}`, `list_replies(since: date, limit=40) -> [RawMail]`
- `smtp.send_smtp(cfg, msg) -> message_id`
- `deliver(cfg, event, voice, prospect, subject, body, kind, today=None, in_reply_to=None, thread_id=None) -> {"message_id","channel":"gmail_api|smtp|manual","eml","mailto","thread_id","error"?}` — always writes the .eml first; a provider failure falls back to manual and reports the error rather than raising
- `RawMail(uid, message_id, from_addr, subject, body, refs, thread_id, received_at)`

### telegram.py
- `esc(text)`; `class Notifier`: `send(text, buttons=None, html_mode=True) -> message_id`, `edit(message_id, text, buttons=None)`, `answer_callback(id, text)`, `force_reply(text) -> message_id`, `info(job, summary)`, `escalate(job, result)`, `error(job, exc)`; `ConsoleNotifier(logfile)`, `TelegramNotifier(token, chat_id, logfile)` (+ `get_updates(offset, timeout)`, `get_me()`), `build_notifier(cfg)`
- `Buttons = list[list[tuple[label, callback_data]]]`

### approvals.py
- `register(prefix, handler, edit_handler=None)`, `register_command(name, fn)` (fn(ctx, args:str) -> str), `apply_verdict(cfg, db, notifier, data, text="") -> str`, `drain(cfg, db, notifier, limit=100) -> int`, `serve(cfg, db, notifier, poll=25)`, `set_pending_edit / pop_pending_edit`, `Context(cfg, db, notifier, prefix, target, verdict, text, callback_id, chat_id)`; ignores updates from any chat other than `cfg.telegram_chat_id`

### approve.py
- `PREFIX="d"`; `buttons(draft_id)`; `draft_message(row, detail) -> str`; `sent_message(row, out, result) -> str`; `register(event, voice, today, regenerate=None)` where `regenerate(draft_row, instruction) -> new_draft_id | None`
- verdicts: `send` (deliver, mark_sent, edit card, next touch), `edit` (force-reply -> correction -> send), `redo` (force-reply -> regenerate with instruction -> new card, old superseded), `skip` (+3 days), `never` (declined + do-not-contact)

### replies.py
- `STOP` regex; `classify(llm, cfg, db, from_addr, subject, body, run_id) -> dict`; `apply_label(db, prospect, label, today) -> str`; `classify_and_apply(...) -> ReplyEvent`; `poll(llm, cfg, db, today, run_id, days=30, limit=40) -> [ReplyEvent]` using `mail.gmail_api.GmailClient.list_replies` or IMAP per `cfg.resolved_replies_provider()`

### job.py
- `class SponsorshipJob(cfg, db, notifier, llm, today, force, event, voice, limit, frozen, poll_replies)` with `run() -> Result|None`; `regenerate(draft_row, instruction)`

### doctor.py
- `Check(name, ok, detail, hint)`; `run_checks(cfg, event=None, voice=None, live=True) -> list[Check]`; `print_report(checks)`

### cli.py
- subcommands: `doctor, demo, run, serve, drain, pending, verdict, status, discover, find-contacts, import, add, export, test-mail, gmail-auth, simulate-reply, freeze, init-db`
- Telegram slash-commands (serve mode): `/pending /status /run [N] /discover /add Company | email | notes /help`

## 5. Guardrails (unchanged from the ancestor, plus one)

| Rule | Enforced by |
|---|---|
| No send without a human verdict | trigger on `drafts.sent_at` |
| `declined` is permanent | trigger on `prospects.status` |
| Never more than 4 cold touches | trigger + CHECK |
| No draft for a closed prospect | trigger |
| No draft to an address that asked us to stop | trigger |
| One live card per prospect | `due_today` excludes prospects with a pending draft |
| Telegram commands only from the configured chat | `approvals._handle_update` |

## 6. Humanization, concretely

Three layers, cheapest first:

1. **Prompt**: the drafter is given `voice.yml` — the sender's sign-off,
   greeting style, tone notes, phrases they use and avoid, and up to three
   real emails they wrote — plus the last three human corrections from the
   `corrections` table as before/after pairs. It is told to write like that
   person, in plain text, under `max_words`.
2. **Lint** (`humanize.lint`, deterministic, free): banned AI phrases
   ("I hope this finds you well", "I'm reaching out", "excited", "delve",
   "leverage", "seamless", "elevate", "unlock", "in today's fast-paced",
   "feel free to", "don't hesitate", "at your earliest convenience", ...),
   more than one em-dash, any exclamation mark, bullet/markdown, three or
   more sentences starting with "I", zero contractions in 60+ words, uniform
   sentence lengths (stdev < 3 words over 5+ sentences), a salesy subject
   ("Partnership Opportunity", "Exciting", "Collaboration Proposal"),
   closing with "Best regards" when the voice says otherwise. Produces a
   0..1 score and issue list.
3. **Rewrite** (`humanize.humanize_pass`, one model call, only when lint
   fails): rewrite in the sender's voice, fix exactly the listed issues,
   keep every claim, add no numbers. The rewritten draft is re-linted and
   re-verified; claims are re-grounded because the body changed.

The verifier's `sounds_human` check is lint score >= 0.7. With
`HUMANIZE_STRICT=1` it is a hard fail; otherwise it is reported on the card.

## 7. Gmail

`gmail_api` (preferred): OAuth2 desktop flow (`python run.py gmail-auth`,
scopes `gmail.send` + `gmail.readonly`), token cached at `data/gmail_token.json`.
Sends land in the user's Sent folder, replies thread properly, and reply
polling uses the same credential — no IMAP password needed.

`smtp` (five-minute path): Gmail app password over STARTTLS 587. Reply
polling then needs IMAP with the same app password.

`manual`: .eml + mailto link + clipboard — nothing leaves the machine.

## 8. Build order

1. Spine (hand-written): config, state, models, store, ladder, tools, agent,
   llm/*, prompts, data examples, conftest.
2. Fan-out (parallel agents, one per module group, each with unit tests):
   research+prospecting+contacts; drafting+humanize+verify; telegram+approvals+approve;
   mail/*; replies; doctor+cli+job wiring; evals+README+CI.
3. Integrate: full test suite, `demo` end-to-end offline, fix.
4. Review: adversarial reviewers (correctness, guardrails/security, humanizer
   quality, Telegram UX, config/secrets), verify findings, fix.
5. Handoff: secrets checklist; `doctor` confirms each one.
