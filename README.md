# SPONSY - sponsorship outreach that never sends without a tap

SPONSY finds companies likely to sponsor your club's event, reads their actual
web pages, writes a short email that sounds like you wrote it, and posts the
draft to your Telegram. You tap **Send**, and only then does it go out through
your Gmail. It then runs the follow-up ladder (day 3, 7, 14), reads the replies,
and stops on its own at day 21. Everything it ever did is in one SQLite file.

---

## The one design decision

Cold email from an unattended student project is how a club's domain gets
blacklisted. So: **nothing is sent without a human verdict, and the gate is in
the database, not in the prompt.** The daily run stops at "here is a draft". A
person taps a button. Only then does an email exist.

If every line of Python in this repo were wrong, these SQLite triggers in
`sponsy/store.py` would still hold the line:

| Rule | Enforced by |
|---|---|
| No send without a human verdict | `trg_send_needs_approval` - `drafts.sent_at` cannot be set unless the verdict is `approved` or `edited` |
| `declined` is permanent | `trg_declined_is_permanent` on `prospects.status` |
| Never more than 4 cold touches | `trg_touch_cap`, `trg_no_draft_past_cap`, plus `CHECK (touches BETWEEN 0 AND 4)` |
| No draft for a closed prospect | `trg_no_draft_when_closed` (declined, cold, interested) |
| No draft to an address that asked us to stop | `trg_no_draft_do_not_contact` |
| A draft can only be sent once | `trg_send_only_once` |
| Nothing enters the outbox without a human verdict | `trg_outbox_needs_approval` |
| Nothing leaves the outbox for a blocked address | `trg_outbox_do_not_contact` - an address blocked (a "stop" reply, a bounce, [Never]) after approval is refused at the last moment |

Two more rules live in code: one live card per prospect (`due_today` skips
anyone with a draft still waiting for a verdict, or an approved email still
in the outbox), and the Telegram bot ignores every chat except the one in
`TELEGRAM_CHAT_ID`.

---

## Try it in thirty seconds, with no keys

```bash
pip install -r requirements.txt
python run.py demo
```

That runs the whole pipeline offline against five frozen source fixtures with a
stub model: research, draft, humanize, verify, queue. Nothing is sent, nothing
touches the network, no Telegram message goes anywhere. Then:

```bash
python run.py --db data/demo.db pending           # what is waiting for a verdict
python run.py --db data/demo.db verdict d:1:send  # approve one
python run.py --db data/demo.db status            # ladder state, run history
```

Or see the same queue in a browser, with buttons:

```bash
python run.py web --demo                          # http://127.0.0.1:8765
```

`verdict d:1:send` writes an `.eml` into `data/outbox/` and prints a `mailto:`
link. Nothing leaves the machine: with no mail provider configured, delivery is
"manual" and you send it yourself. The ladder still schedules the next touch.

---

## Setting it up for real

Copy `.env.example` to `.env`. Fill in the pieces below, in order. Each one is
independent; `python run.py doctor` tells you which are working.

1. **Telegram** (the phone frontend; `python run.py web` is the browser one).
   Open Telegram, talk to `@BotFather`, `/newbot`, copy the token into
   `TELEGRAM_BOT_TOKEN`. Then send your new bot any message (it cannot message
   you first). Run `python run.py doctor`: it reads that message and prints your
   chat id. Put it in `TELEGRAM_CHAT_ID`. The bot only ever answers that chat.

2. **A model key.** One of three; `LLM_PROVIDER=auto` picks the first with a key
   in this order.
   - **Gemini** - `GEMINI_API_KEY` from aistudio.google.com. The free tier is
     about 20 requests per day per model, and one prospect costs 5 to 7 requests
     (research loop, draft, maybe a humanize pass and a regeneration), so a free
     key does roughly **3 prospects a day**. `LLM_MIN_INTERVAL=6.5` paces calls
     under the per-minute limit; put extra keys in `GEMINI_API_KEYS`
     (comma-separated) and the client rotates to the next one when a key hits
     its daily cap. A daily-quota 429 stops the run with a clear message instead
     of retrying for an hour.
   - **Any OpenAI-compatible endpoint** - `OPENAI_API_KEY` plus, for anything
     that is not OpenAI itself, `OPENAI_BASE_URL` and `LLM_MODEL`. Works with
     OpenAI, NVIDIA NIM (free endpoints at build.nvidia.com:
     `OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1` and `LLM_MODEL` set to
     a NIM model id), Groq, OpenRouter and Featherless.
   - **Anthropic** - `ANTHROPIC_API_KEY`. Defaults to `claude-opus-5` for
     research and drafting and `claude-haiku-4-5` for the cheap calls (reply
     classification, the groundedness judge). Server-side refusal fallbacks are
     on by default for the Opus calls; `ANTHROPIC_FALLBACKS=0` turns them off.
     Needs `pip install anthropic` (it is in `requirements.txt`).

3. **Tavily** - `TAVILY_API_KEY` from tavily.com, free tier about 1000 searches a
   month. Strongly recommended. Without a search key the research falls back to
   scraping DuckDuckGo, which is fragile: on the ancestor's first live run it
   returned a random GitHub list instead of the company's own site, which both
   wrecked the draft and doubled the model requests spent looking.

4. **Gmail**, one of two ways.
   - **Option A, Gmail API** (preferred: sends land in your Sent folder, replies
     thread properly, reply polling uses the same credential, no password is
     stored). Google Cloud Console > New project > APIs & Services > Enable
     "Gmail API" > OAuth consent screen (External; add your own address as a test
     user) > Credentials > Create credentials > OAuth client ID > Desktop app >
     Download JSON. Save it as `data/gmail_credentials.json`, then run
     `python run.py gmail-auth` once. A browser opens, you approve, and the token
     is cached at `data/gmail_token.json`. Both files are gitignored.
   - **Option B, app password** (five minutes, no Cloud Console). With 2-step
     verification on, go to myaccount.google.com/apppasswords and create one.
     Put your address in `SMTP_USER` and the 16-character password in
     `SMTP_PASSWORD`. The same address and password go in `IMAP_USER` /
     `IMAP_PASSWORD` so replies can be read.

   `MAIL_PROVIDER=auto` picks the Gmail API if a token or credentials file
   exists, else SMTP if a password is set, else manual. `ALLOW_SEND=0` forces
   manual for a while, which is a good setting for your first week.

5. **`data/event.yml`** - copy `data/event.example.yml`. The drafter asserts
   every line in that file as fact, and the verifier treats those facts as
   grounded by construction. So if a number in there is not true, it will end up
   in an email. It is gitignored because it carries a phone number. The
   example also fills the HTML email's fixed blocks (stats, what we offer,
   past sponsors, organisers, contacts), and `past_sponsors` decides who gets
   the welcome-back first email instead of a cold one.

6. **`data/voice.yml`** - copy `data/voice.example.yml`. This is what makes the
   emails sound like you and not like a model: your greeting, your exact
   sign-off, notes on how you write, phrases you use and phrases you would never
   write. **The `sample_emails` block matters most**: paste up to three emails
   you genuinely wrote, verbatim, typos and all. The drafter imitates them, and
   the humanizer checks against them. If the file is missing, a plain voice is
   built from `event.yml`.

7. **`data/brochure.pdf`** (optional). Drop your sponsorship brochure there and
   it is attached automatically — to the **first** email and to any reply that
   answers a question, and deliberately **not** to the nudges: re-sending the
   same PDF three times is how a thread starts looking like spam, and it costs
   deliverability on every later email from that address. Point `BROCHURE`
   somewhere else if you keep it elsewhere; leave the file absent and the
   emails simply go out as plain text. Keep it under 15 MB. The drafter is told
   when it is attached, so the copy says "the brochure is attached" instead of
   offering to send one. Note that in **manual** mode the `mailto:` link cannot
   carry a file — open the `.eml` from `data/outbox/` instead, which has it.

### Testing from your own address first

Do this before the club account. It takes two minutes and it is the only way
to see what a sponsor actually receives.

```bash
# 1. In .env, point the mail at your personal Gmail and fence it in:
#      SMTP_USER=you@gmail.com
#      SMTP_PASSWORD=<16-char app password from myaccount.google.com/apppasswords>
#      SMTP_FROM=you@gmail.com
#      MAIL_PROVIDER=smtp
#      SEND_ONLY_TO=you@gmail.com
python run.py doctor                       # mail should go OK
python run.py test-mail --to you@gmail.com # one real email, to yourself
```

`SEND_ONLY_TO` is the part that matters. While it lists addresses, **[Send]
only reaches those** — tap it on a real prospect's card by accident and the
email is written to `data/outbox/` and goes nowhere. The dangerous moment in
this whole project is the one where mail starts working for the first time
and a researched card for a real company is still sitting in the chat one tap
away. Clear the line when you are done testing; `doctor` reminds you it is on.

To exercise the whole pipeline rather than one message, add yourself as a
prospect and let the agent write to you:

```bash
python run.py add "Test Co" --email you@gmail.com --contact "Your Name"
python run.py run --limit 1                # researches, drafts, posts the card
# tap Send in Telegram -- it lands in your own inbox, brochure and all
```

Then swap `SMTP_USER` / `SMTP_FROM` and `IMAP_USER` / `IMAP_PASSWORD` to the
club address (or set up the Gmail API for it), point `reply_to` in
`event.yml` at that same inbox, clear `SEND_ONLY_TO`, and delete the test
prospect. `reply_to` becomes the `Reply-To` header: aim it anywhere the bot
does not read and replies never reach it, so the ladder keeps nudging people
who already answered.

8. **`python run.py doctor`** is the gate. It checks every secret live: the
   Telegram token and chat, the model key with one tiny call, the search
   provider, the mail channel, reply polling (including that `reply_to` is the
   inbox it polls), and both YAML files. Do not start a live run until it is
   clean.

---

## Running it

```bash
python run.py import data/prospects.csv   # load the list (never resets state)
python run.py run --limit 3               # first live run: 3 prospects
python run.py serve                       # keep this up: buttons react instantly
```

`run` is the daily job: it applies verdicts you tapped since the last run,
polls replies, sweeps day-21 prospects to cold, then researches and drafts for
whoever is due. It runs at most once per calendar day unless you pass
`--force`. Every draft ends up as a Telegram card.

`serve` long-polls Telegram so the card buttons and the `/commands` work the
moment you tap them, sends the outbox at the paced rate (below), and with
`--daily 09:30` (or `DAILY_RUN_AT` in `.env`) runs the daily job itself, so a
server needs no cron. Run it on a laptop, a Raspberry Pi, a free VM, or in
Docker (`docker compose up -d`) - anything that stays on and can reach
Telegram. **For a server, follow [deploy/README.md](deploy/README.md)**:
Docker or systemd, the Gmail token on a headless box, the HTTPS unsubscribe
link, health checks and backups. On Windows, `scripts\serve.bat` does it; register that with Task
Scheduler "at log on" if you want it permanent.

`drain` is for cron-only setups with no process running: it applies the
verdicts you tapped since the last pass and exits. `run` calls the same thing
first, so on a pure cron deployment your overnight taps are applied at the
start of the morning run.

**Daily automation, two ways.**

- Windows Task Scheduler: Create Basic Task > Daily at 10:00 > Start a program
  > `E:\path\to\SPONSY_BOT\scripts\run.bat` (first argument is `--limit`,
  default 5). `scripts\doctor.bat` is a double-clickable health check. The
  scripts use `.venv\Scripts\python.exe` when it exists (`python -m venv .venv`,
  then `.venv\Scripts\pip install -r requirements.txt`), which sidesteps the
  Microsoft Store `python` alias on a fresh Windows install.
- GitHub Actions: `.github/workflows/daily.yml` runs at 10:00 IST and keeps the
  database in the Actions cache between runs. Add the model, Telegram, Tavily
  and (optionally) IMAP keys as repository secrets, plus `SPONSY_EVENT_B64` /
  `SPONSY_VOICE_B64` (`base64 -w0 data/event.yml`) for the two gitignored
  files. **OAuth tokens and sending do not belong in CI.** The workflow has no
  SMTP password, no Gmail token, and sets `ALLOW_SEND=0`, so CI can only draft.
  You tap and send from `serve` on your own machine. Keep `serve` running
  before you tap: if the CI run drains a Send verdict first, it resolves to
  manual delivery (an `.eml` in the run artifact, nothing actually sent) and
  the draft is marked sent.

---

## Staying out of spam

Receivers judge a sender on four things: whether the domain authenticates
(SPF, DKIM, DMARC), whether it sends like a person (no bursts, a gradual
ramp), whether its list is clean (bounces), and whether recipients want the
mail (spam reports). SPONSY handles the parts that are code:

- **Paced sending.** With a real provider, [Send] puts the email in an
  outbox. It leaves at once if a slot is free, otherwise at the next one, and
  the card says when. The defaults are at most 20 a day, a warm-up from 8 a
  day, 90 s+ between sends, and weekdays 09:00-18:00. The caps are in `.env`
  (`SEND_*`).
- **Bounces stop the ladder.** A domain that cannot receive mail is skipped
  before drafting (one DNS lookup). A bounce report is recognised in code, and
  the address is blocked and its follow-ups cancelled. If the bounced email
  was the only one sent, its touch is given back. If more than 5% of the
  week's mail bounces, sending pauses itself.
- **Failures are read, not retried blindly.** "Slow down" (421, 4.7.28, 429)
  pauses everything for as long as the provider asked. "Spam" or
  "unauthenticated" (5.7.x) and broken credentials pause until you `/resume`.
  "No such user" is treated as a bounce. A network blip retries 2, 4, 8 ...
  minutes later with the same bytes and the same `Message-ID`.
- **An easy way out.** Every email has a `List-Unsubscribe` header and a
  polite opt-out line (`opt_out` in event.yml). With `PUBLIC_URL` set, it
  also carries a signed RFC 8058 one-click link that Gmail turns into its own
  Unsubscribe button. Unsubscribing is far cheaper for your reputation than a
  spam report.
- **Follow-ups thread under the first email** (`In-Reply-To`, `References`,
  `Re: <first subject>`) instead of arriving as new cold emails.
- **Warnings before you tap.** The card flags fake `Re:`, link shorteners,
  ALL-CAPS, salesy phrases and too many links. `doctor` checks your domain's
  SPF, DKIM, DMARC and MX, the template's size (Gmail clips HTML over 102 KB),
  and the pacing settings.

What it deliberately does not do: rotate domains, hide text, track opens,
shorten links, or probe mailboxes. Receivers treat each of these as a spam
signal. The DNS records you need, and Google Postmaster Tools, are in
[deploy/README.md](deploy/README.md#1-dns-for-your-sending-domain).

`/outbox`, `/pause`, `/resume` and `/cancel N` in Telegram, the Activity tab
on the web page, and `python run.py outbox [--wait|--pause|--resume]` all show
and steer the queue. The manual channel and `--dry-run` skip the outbox:
nothing leaves the machine, so there is nothing to pace.

---

## The web UI

```bash
python run.py web            # http://127.0.0.1:8765  (--open to launch a browser)
python run.py web --demo     # offline, on data/demo.db with the frozen fixtures
```

The same approval queue as Telegram, plus the parts a phone is bad at:

- **Queue** - every draft waiting for a verdict, with the numbers the card
  shows (words, claims traced to sources, human-score, the brochure) and the
  claims it made, each marked traced or not, and any spam-filter risk in the
  wording. Send, Edit (rewrite it in place; the before/after pair becomes a
  correction the drafter learns from), Redo (say what should change), Skip,
  Never. Send asks first and says what will actually happen: a real email
  (now, or at its paced slot), a held `.eml` under `SEND_ONLY_TO`, or a manual
  `.eml` when no provider is set up.
- **Prospects** - the list with status, ladder position and next touch;
  search, filter (including "Needs address", which counts bounced addresses),
  add one, export a CSV. Click a row for everything SPONSY knows about them:
  the research summary, every draft and its verdict (or its outbox slot), the
  replies and bounces, the pages it read, and the audit log.
- **Activity** - the outbox (what is waiting, when it goes out, sent today
  against the cap, bounces this week; Pause, Resume, Send what is due, Cancel
  one), every email sent and what came back, and the run history.
- **Agent** - the daily run, Discover (pick which companies to add), Find
  addresses (pick which candidate to keep) and the doctor, with live output.

It is not a second set of rules. Every button goes through the same
`approvals.apply_verdict` the terminal and Telegram use, so the triggers gate
it identically, and when Telegram is configured a card you decide on here is
rewritten in place in the chat. Verdicts are disabled while an agent task is
running. Run it next to `serve` if you like; they share the database.

It binds to `127.0.0.1` and refuses requests addressed to any other host
name. To reach it from another machine (or from Docker:
`docker compose --profile web up -d`), set `SPONSY_WEB_TOKEN` and pass
`--host 0.0.0.0`; it will not listen beyond loopback without one, because
this page can send email. Standard library only: no new dependencies, no
build step. On Windows, `scripts\web.bat` starts it and opens a browser.

---

## The Telegram card

Every draft arrives as one card: company and prospect id, the touch kind,
subject, the full body, a line saying how many words, how many claims traced to
how many sources, and the **human score** (the AI-tell lint, 1.0 = clean). If
the verifier failed, the card says `UNVERIFIED` and lists which checks failed
and which claims it could not ground; it is still posted, never silently
dropped, because you may know something the sources did not say.

Five buttons:

| Button | What happens |
|---|---|
| **Send** | Delivered through the Gmail API or SMTP (or written as `.eml` + `mailto:` if neither is set up). The draft is marked sent, the card is edited to say so, and the ladder schedules the next touch. |
| **Edit** | The bot asks you to reply with your version. Your rewrite is stored next to the original in the `corrections` table, then sent. The last three corrections are shown to the drafter as before/after pairs on every later draft, so it learns your taste. |
| **Redo** | The bot asks what to change ("shorter", "lead with the workshop, not the prize"). A new draft is written for the same prospect following that instruction, a new card is posted, and the old one is retired. The instruction and both drafts are kept as a correction pair too. |
| **Skip** | Not now. The prospect comes back in three days. |
| **Never** | Marked `declined` (permanent) and the address goes on the do-not-contact list. Any other pending draft for them is cancelled. |

Slash commands, in the same chat while `serve` is running:

| Command | Does |
|---|---|
| `/pending` | drafts waiting for you |
| `/status` | prospects by status, sent count, blocked count, who is waiting on an address |
| `/run [N]` | research and draft N prospects now |
| `/discover` | find companies that sponsor events like yours and add them (no address yet) |
| `/add Company \| email \| notes` | add one prospect (email and notes optional) |
| `/never P01` | never contact this prospect again |
| `/help` | the list above |

Messages from any chat other than `TELEGRAM_CHAT_ID` are ignored.

---

## How it decides what to write

**Research is a closed world.** The agent loop has two tools, `web_search` and
`fetch_page`, and a hard cap of `MAX_FETCHES=4` pages per prospect enforced
inside the tool, not in the prompt. Every fetched page is stored in the
`research` table with its URL, and the drafting prompt contains that research
plus the fetched excerpts plus your `event.yml` facts plus your voice, and
nothing else. No web access, no memory of other companies. Research runs once
per prospect, not once per touch: a follow-up reuses the same pages instead of
refetching four of them to say "just following up".

**Claims and the verifier.** The drafter must list every factual assertion it
made about the company in a `claims` array. That turns "is this email true?"
into a per-claim lookup: `grounded()` checks each claim against the excerpts,
keyword overlap first (free), a cheap model call only for the ambiguous middle
(`GROUNDING=hybrid`). Any number in a claim that does not appear in the sources
fails it outright; invented statistics are the failure mode that matters and
the cheapest one to catch. Twelve deterministic checks run on top: length,
names the company, has an ask, no placeholders, no filler, a quiet subject,
claims traceable, no superlatives, greets our contact (not a byline lifted
from a blog post), has claims, not empty, sounds human. A failing draft gets
one regeneration with the checklist as feedback; if that fails too it is
queued flagged `UNVERIFIED`.

**Three humanization layers, cheapest first.**

1. *Prompt.* The drafter gets `voice.yml` (greeting, sign-off, tone notes,
   phrases you use and avoid, your real emails) and the last three corrections
   you made, and is told to write like that person, in plain text, under
   `max_words`.
2. *Lint* (`sponsy/humanize.py`, deterministic, free). Banned AI phrases ("I
   hope this finds you well", "I'm reaching out", "excited", "delve",
   "leverage", "seamless", "elevate", "unlock", "feel free to", "don't
   hesitate", ...), more than one em-dash, any exclamation mark, bullets or
   markdown, three or more sentences starting with "I", no contractions in a
   60+ word body, suspiciously uniform sentence lengths, a salesy subject
   ("Partnership Opportunity", "Exciting", "Collaboration Proposal"), and a
   sign-off that is not yours. It produces the 0..1 score shown on every card.
3. *Rewrite* (one model call, only when the lint fails). Fix exactly the listed
   issues in the sender's voice, keep every claim, add no numbers. The result is
   re-linted and re-verified, and claims are re-grounded because the body
   changed.

The verifier's `sounds_human` check is lint score >= 0.7. By default it is
reported on the card and does not block; with `HUMANIZE_STRICT=1` it is a hard
fail and the draft is queued `UNVERIFIED`. `HUMANIZE=0` switches the lint and
the rewrite off.

---

## The ladder and the replies

Timing lives in `sponsy/ladder.py`, in code. The model only writes copy.

| Touch | Day | Kind | What changes |
|---|---|---|---|
| 1 | 0 | `initial` | the researched hook |
| 1 | 0 | `returning` | *instead of* `initial` when they backed a past edition |
| 2 | +3 | `nudge` | three or four lines, exactly one new detail |
| 3 | +7 | `new_angle` | different frame (recruiting vs visibility), maybe a smaller tier |
| 4 | +14 | `last_touch` | short, no pressure, says plainly it is the last one |
| - | +21 | `cold` | auto-marked, stops |

Four touches, ever. A `Skip` pushes the next touch by three days; "no sources
found" retries tomorrow rather than drafting blind.

**The returning variant.** A company listed in `past_sponsors` gets a different
touch 1: it opens on the existing relationship, skips the who-we-are paragraph
and the why-we-picked-you paragraph, and asks them to come back. Sending the
cold version to a company whose logo is already on your own site reads as
though you forgot they sponsored you, which is a worse impression than not
writing. Matching is on the normalised name, so `Digital Ocean` in the prospect
sheet finds `DigitalOcean` in the config. The verifier makes the relationship a
*hard* check — a returning draft that never mentions it is rejected rather than
sent — and the approval card labels it, because its premise is a claim about
your own history that only you can confirm. Set `whats_new` in `event.yml` to
give it a reason beyond the edition number; left blank, the drafter is told to
use the edition number rather than invent one.

Replies are polled read-only (Gmail API or IMAP), classified, and turned into a
status change:

| Label | What happens |
|---|---|
| `interested` | ladder cancelled, you get a Telegram message with the snippet, a human takes over |
| `declined` | marked `declined` (permanent), address blocked, pending drafts cancelled |
| `needs_info` | a reply answering their question is drafted and queued for your approval |
| `out_of_office` | next touch pushed back; not counted as a touch |
| `other` | recorded, nothing changes |

"Stop contacting me" never reaches the model: it is a regex, then a permanent
block. When the classifier is torn between `declined` and `needs_info` it is
told to pick `declined`; being wrong that way costs one prospect, being wrong
the other way means emailing someone who asked us to stop.
`python run.py simulate-reply P01 --text "no budget this year"` exercises all
of this without a mailbox.

---

## Finding prospects

`data/prospects.csv` is the list. Columns: `prospect_id`, `company` (both
required), `contact_name`, `email`, `segment`, `status`, `website`, `notes`.
Re-importing refreshes contact details and never resets state; a blank cell
keeps what the database already has, so an address `find-contacts --apply` or
`add` found survives a re-import of the list. Rows with no email address are
parked: the agent will not spend model quota researching someone it cannot
write to, and `status` lists who is waiting on an address.

```bash
python run.py discover                    # who sponsors events like ours?
python run.py discover --apply            # add them to the list (no address yet)
python run.py discover --query "fintech hackathon sponsors India" --apply
python run.py find-contacts               # look for a published address for each
python run.py find-contacts --apply       # accept the top candidate per prospect
python run.py add "Acme" --email hello@acme.example --contact "Devi" --notes "..."
```

`discover` reads the sponsor pages of comparable events (queries are built from
your event's kind and region) and ranks companies by how many sponsor pages
they appear on. `find-contacts` looks for a published role mailbox
(sponsorships@, partnerships@, devrel@) on the company's own pages and fails
closed: if it cannot find one it says so and points you at their form, rather
than guessing. Nothing is written without `--apply`.

---

## Every command

All commands take `--db PATH`, `--date YYYY-MM-DD` (pretend today is another
day; this is how the multi-week ladder gets tested in seconds), `--offline`
(fake model, no search, no mail, console output) and `--dry-run` (never send
even if a provider is configured).

| Command | Does |
|---|---|
| `doctor` | check every secret and integration live; prints your Telegram chat id |
| `demo [--limit N]` | offline end-to-end run on the frozen fixtures into `data/demo.db` |
| `run [--limit N] [--force] [--no-replies] [--fixtures DIR]` | the daily run |
| `serve [--limit N] [--daily HH:MM]` | long-poll Telegram: buttons and `/commands`, send the outbox, and (with `--daily`) run the daily job at that local time |
| `outbox [--send] [--wait] [--pause [REASON]] [--resume] [--cancel ID]` | approved emails waiting for a send slot; `--wait` keeps sending at the paced rate until the queue is empty |
| `backup [--keep N]` | consistent copy of the database to `data/backups/` |
| `healthcheck [--url URL]` | exit 0 if `serve` wrote its heartbeat recently (or the web server answers `/healthz`) |
| `web [--host H] [--port N] [--limit N] [--token T] [--demo] [--open]` | the browser UI on localhost: queue, prospects, activity, agent |
| `drain` | apply verdicts tapped in Telegram since the last pass, then exit |
| `pending` | drafts waiting for a verdict, with the verdict command for each |
| `verdict d:ID:send\|skip\|never\|edit\|redo [--text ...]` | approve from the terminal (`--text` is the rewrite for `edit` or the instruction for `redo`) |
| `status` | ladder state, run history, costs, channels in use |
| `discover [--query Q]... [--limit N] [--apply]` | find companies that sponsor events like yours |
| `find-contacts [--limit N] [--apply]` | find published addresses for prospects that have none |
| `import CSV` | upsert prospects from a CSV |
| `add COMPANY [--email] [--contact] [--segment] [--website] [--notes]` | add one prospect |
| `export [CSV]` | write prospects and their state to a CSV |
| `test-mail` | send one test email to yourself through the configured channel |
| `gmail-auth` | run the Gmail OAuth flow once |
| `simulate-reply PID --text "..." [--subject S]` | feed a reply in without a mailbox |
| `freeze PID` | write what was actually fetched for a prospect as an eval fixture |
| `init-db` | create the database and stop |

Evals:

```bash
python evals/run_evals.py                    # offline, free, deterministic
python evals/run_evals.py --live             # real model on the frozen sources
python evals/run_evals.py --inject-failure   # kill the first fetch of each case
```

Five cases in `data/fixtures/`, each a company plus frozen source excerpts.
`P05` has deliberately thin sources; the correct behaviour there is a shorter,
vaguer draft that asks a question, not a confidently invented one. The report
prints per-case rows and aggregate rates (task success on every criterion,
groundedness, thin-source handling, human rate, median cost and steps) and
writes `evals/results/sponsorship-<date>.json`.

---

## Layout

```
run.py                     entry point -> sponsy.cli.main
sponsy/
  config.py                .env loader; Config, Event (event.yml), Voice (voice.yml)
  state.py                 SQLite wrapper; runs / actions / memory (the audit log)
  models.py                Prospect, Excerpt, Research, Draft, ReplyEvent; status machine
  store.py                 sponsorship tables, the eight triggers, all queries
  ladder.py                touch timing (0/3/7/14, cold 21), 4-touch cap
  tools.py                 provider-neutral tool registry
  agent.py                 run_agent() tool loop + structured() one-shot JSON
  prompts.py               every prompt, one file
  llm/                     gemini.py, openai_compat.py, anthropic_.py, fake.py
  research.py              search providers, robots-aware fetch, excerpting
  prospecting.py           discover companies from sponsor pages of comparable events
  contacts.py              find a published role mailbox, fail closed
  drafting.py              closed-world drafter with voice + few-shot corrections
  humanize.py              AI-tell lint + rewrite pass
  verify.py                checklist, grounded(), greeting check, confidence
  replies.py               classification + status transitions; Gmail/IMAP transports
  mail/                    deliver()/prepare()/transmit(), message building, gmail_api.py,
                           smtp.py; pacing.py (send-rate policy), dns.py (MX, SPF, DKIM,
                           DMARC over DoH), bounces.py, errors.py (what a failure means),
                           spamcheck.py
  outbox.py                approved emails, sent at the paced rate; retries, pauses, recovery
  unsubscribe.py           List-Unsubscribe headers and the signed one-click link
  runtime.py               SIGTERM handling, heartbeat, the daily schedule, backups
  telegram.py              Bot API client, console fallback
  approvals.py             verdict routing, drain()/serve(), slash-commands, chat allowlist
  approve.py               card formatting, buttons, send/edit/redo/skip/never
  job.py                   SponsorshipJob: gather -> act -> verify -> report
  doctor.py                live checks of every integration and secret
  web.py                   browser UI: JSON API over http.server, same verdict path
  static/                  the page: index.html, app.js, style.css (no build step)
  cli.py                   subcommands
  fakes.py                 offline model stubs
data/
  event.yml                the event (gitignored)         event.example.yml
  voice.yml                how you write (gitignored)     voice.example.yml
  prospects.csv            the list
  fixtures/*.json          frozen sources for demo/tests/evals
  outbox/                  every approved email as .eml + notifications.log
  gmail_credentials.json   OAuth client (gitignored)      gmail_token.json (gitignored)
tests/                     offline; FakeLLM; no network
evals/run_evals.py         frozen-source eval harness
scripts/*.bat              run / serve / doctor for Task Scheduler
.github/workflows/daily.yml
Dockerfile, docker-compose.yml
deploy/                    server guide, Caddyfile, systemd units, container entrypoint
```

---

## Known limitations and cost

- **Free-tier quota is the real bottleneck, not the code.** A free Gemini key
  is about 3 prospects a day. Add keys to `GEMINI_API_KEYS`, use a NIM free
  endpoint, or enable billing (a full run is cents).
- **`fetch_page` cannot read JS-rendered pages.** It reports that rather than
  pretending, and research confidence drops accordingly. Sites that block bots
  or disallow us in `robots.txt` are skipped, not scraped.
- **DuckDuckGo fallback search is fragile.** It is HTML scraping. Get a Tavily
  key for anything you care about.
- **Reply matching is best-effort for manual sends.** When the send went
  through the Gmail API or SMTP we control the `Message-ID` and the thread, so
  replies match. A manual (`.eml` / `mailto:`) send falls back to matching on
  the sender's address, which misses a reply from a colleague's address.
- **A model will greet the wrong person given the chance.** Salutations are not
  claims, so groundedness never sees them; `greets_our_contact` is a separate
  deterministic check because of that.
- **GitHub Actions cache is best-effort storage.** If an entry is evicted
  mid-ladder, prospects with a `next_touch` in the past are picked up again on
  the next run, but touch counts reset. Point `SPONSY_DB` at durable storage,
  or run it on a machine of your own, if that matters.
- **Cost figures are estimates.** The price table in `sponsy/config.py` is a
  best guess per million tokens; check current pricing before quoting a number.
  `LLM_MAX_STEPS=8` and `LLM_MAX_COST=0.05` cap the research loop per prospect;
  hitting a cap ends the loop and lowers confidence rather than throwing the
  work away. Gemini thinking is off by default (`LLM_THINKING_BUDGET=0`); turn
  it on if drafts feel flat and watch the median cost in `status`.
- **The eval numbers measure the pipeline, not the model**, unless you ran
  `--live`. The offline drafter is a deterministic stub. The shipped fixture
  excerpts are hand-written placeholders; replace them with
  `python run.py freeze P01` output from a real run before quoting
  groundedness anywhere.
- **Not built, on purpose:** auto-send (see the top of this file), a web UI
  (Telegram is the whole frontend), a graph framework (one short loop and a
  deterministic verifier), a learned escalation policy (forty prospects is not
  a dataset). Every approve, skip, edit and redo is stored, though, and after a
  few weeks of use `corrections` is a labelled set of your own taste.
