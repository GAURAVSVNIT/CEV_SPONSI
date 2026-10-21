# Running SPONSY on a server

This covers two things: keeping the agent running on a small server, and
keeping its email out of spam. The second matters more, and most of it is DNS
and sending habits, not code. The code below enforces the habits.

## What keeps mail out of spam

Gmail, Outlook and Yahoo don't decide on words alone. They mostly ask four
questions about the sender:

| Question | What SPONSY does | What you do |
|---|---|---|
| **Is it really you?** (SPF, DKIM, DMARC) | `doctor` looks the records up and says what is missing | Publish the records (below) |
| **Do you send like a person?** (rate, bursts, volume) | The outbox sends at most `SEND_DAILY_CAP` a day, warms up from `SEND_WARMUP_START` a day, keeps sends `SEND_MIN_GAP`+ seconds apart, and only sends during `SEND_HOURS` on `SEND_DAYS` | Keep the defaults for the first weeks |
| **Is your list clean?** (bounces) | Checks a domain can receive mail before drafting to it. Reads bounce reports, blocks dead addresses and stops their follow-ups. Pauses all sending when more than 5% of the week's mail bounces | Use real, published addresses; fix the ones it flags |
| **Do recipients want it?** (spam reports) | Adds a `List-Unsubscribe` header and a one-click link (RFC 8058), plus a polite opt-out line, so a recipient can unsubscribe instead of reporting spam. An opt-out reply is honoured without asking the model | Set `PUBLIC_URL` so the one-click link works |

Google's own bar is a spam-report rate under 0.1% and never 0.3%. Bulk
senders (5,000+ a day to Gmail) must also pass SPF, DKIM and DMARC, include
one-click unsubscribe, and use TLS. SPONSY sends a few dozen a day, far below
that line, but it meets the same rules anyway: they are what a receiver checks
before it decides how much to trust you.

**What it deliberately does not do:** rotate domains or IPs, hide text, add
tracking pixels, shorten links, or probe mailboxes with SMTP "verification".
Receivers treat each of these as a spam signal, and none of them are needed
for a few honest emails a day.

## 1. DNS for your sending domain

Skip this if you send from a plain `@gmail.com` address: Google
authenticates that itself. For a club domain (recommended once you send more
than a handful a day), on Google Workspace:

```text
TXT  @         "v=spf1 include:_spf.google.com ~all"
TXT  google._domainkey   (Admin console > Apps > Google Workspace > Gmail >
                          Authenticate email > Generate new record, 2048-bit,
                          then click "Start authentication")
TXT  _dmarc    "v=DMARC1; p=none; rua=mailto:dmarc-reports@yourclub.org"
MX   @         (Workspace's MX records, so replies and bounces come back)
```

Start DMARC at `p=none` and read the reports for a couple of weeks before you
tighten it to `quarantine`. Then check everything:

```bash
python run.py doctor          # the "sender domain" line
```

Optional but worth it: add the domain to [Google Postmaster
Tools](https://postmaster.google.com). It shows your spam rate and domain
reputation once you send enough for Google to report.

## 2. A server

Any small Linux VPS (1 vCPU, 1 GB RAM) is plenty. With Docker:

```bash
git clone <this repo> sponsy && cd sponsy
cp .env.example .env            # fill it in; set TZ, DAILY_RUN_AT, SPONSY_WEB_TOKEN
cp data/event.example.yml data/event.yml   # and voice.yml; or copy yours over
docker compose build
docker compose run --rm sponsy python run.py doctor
docker compose up -d                       # serve: Telegram + outbox + daily run
docker compose --profile web up -d         # optional: UI + unsubscribe endpoint
docker compose logs -f
```

Without Docker, use the systemd units in this folder (`sponsy.service`,
`sponsy-web.service`); the install steps are at the top of `sponsy.service`.

### The Gmail token on a headless server

`gmail-auth` needs a browser. Run it once on your laptop, then copy the token:

```bash
python run.py gmail-auth                    # on the laptop
scp data/gmail_token.json server:sponsy/data/
```

In the Google Cloud console, set the OAuth consent screen's publishing status
to **In production**. In "Testing", Google expires the refresh token after 7
days and sending stops. You will see an "unverified app" warning when you
authorise your own account; that is fine for a single account. If the token
does break, the outbox pauses itself and Telegram tells you; re-run
`gmail-auth` on the laptop, copy the token again, then `/resume`.

The simpler alternative for a server is SMTP with a Gmail **app password**
(`MAIL_PROVIDER=smtp`, `SMTP_USER`, `SMTP_PASSWORD`, plus IMAP for replies).

### Time zone

`SEND_HOURS` and `DAILY_RUN_AT` are local times. Set `TZ=Asia/Kolkata` (or
yours) in `.env`; the image includes the zone data. `doctor` prints the
policy with its zone so you can check.

## 3. The one-click unsubscribe link (recommended)

Every email already carries a `mailto:` unsubscribe header. The one-click
form, which Gmail shows as an "Unsubscribe" button, needs an HTTPS address
the recipient's mail provider can POST to:

1. Run the `web` service (`--profile web`) and set `SPONSY_WEB_TOKEN`.
2. Point a DNS name at the server and install [Caddy](https://caddyserver.com),
   which handles the certificate on its own.
3. Use `deploy/Caddyfile`. It publishes only `/u/...` (the signed unsubscribe
   links) and `/healthz`. Everything else returns 404.
4. Set `PUBLIC_URL=https://that.name` in `.env` and restart.

Reach the UI itself through an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 you@server      # then open http://127.0.0.1:8765
```

The links are HMAC-signed with a key kept in `data/secret.key` (or
`SPONSY_SECRET`), so nobody can unsubscribe someone else by guessing ids. A
GET only shows a confirm button: link scanners open every URL in an email,
and must not unsubscribe anyone.

## 4. Day to day

| | |
|---|---|
| `/outbox` (Telegram), the Activity tab, `python run.py outbox` | What is waiting, when it goes out, sent today vs the cap, bounces this week |
| `/pause`, `/resume` | Stop or restart sending. Drafting and approvals carry on |
| `/cancel N` | Drop one queued email before it leaves |
| `python run.py backup` | Consistent copy of the database to `data/backups/`. `serve --daily` also takes one every day before the run |
| `docker compose ps` | `healthy` means `serve` wrote its heartbeat recently (it does so between polls; the check allows 30 minutes, the longest a daily run may hold the loop) |

Sending pauses itself, and tells you why, when:

- the provider says slow down (Gmail 421 / 4.7.28 / 429 / daily quota). It
  resumes on its own after the wait the provider asked for.
- a message is refused as spam or as unauthenticated (550 5.7.x). Run
  `doctor`, fix what it reports, then `/resume`.
- the mail login breaks (expired token, wrong app password). Fix it, then
  `/resume`.
- too many of the week's emails bounce. Check the addresses, then `/resume`.

Nothing is lost while paused: approved emails wait in the outbox. A send
interrupted by a restart is checked against Gmail's Sent folder before it is
retried, and a retry re-sends the same bytes under the same `Message-ID`.

## 5. Warming up a new address

A brand-new account or domain that starts at full volume looks like a
spammer's throwaway. The defaults handle this: 8 on the first day, +4 a day,
up to 20. For the first week or two, also:

- send the first emails to people likely to reply (past sponsors first: the
  ranked list already does this), because replies build reputation fastest;
- keep the brochure attached only to the first email (the default), and
  don't raise `SEND_DAILY_CAP` until bounces stay near zero.

## Updating

```bash
git pull
docker compose build && docker compose up -d   # the outbox and data survive
```
