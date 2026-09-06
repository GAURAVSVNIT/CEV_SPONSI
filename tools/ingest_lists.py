#!/usr/bin/env python
"""Merge the club's sponsor spreadsheets into one ranked prospect list.

    python tools/ingest_lists.py <file> [<file> ...] [--apply]

The lists come from different events with different audiences -- a TEDx sheet
of Gujarat pharma companies, an MLH hackathon sheet, a fest sheet of media
buyers -- so the job here is not just deduplication. It is deciding which of
them belong anywhere near a 36-hour developer hackathon, and in what order.

Nothing is written to the database without --apply, and even then nothing is
ever mailed: every prospect still has to come round through a run, a card and
a tap.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sponsy.config import DATA, Event  # noqa: E402
from sponsy.store import norm_company  # noqa: E402

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FREE_MAIL = ("gmail.com", "yahoo.com", "yahoo.co.in", "rediffmail.com",
             "hotmail.com", "outlook.com", "protonmail.com")
GENERIC_LOCAL = ("info", "contact", "support", "enquiry", "enquiries",
                 "inquiry", "customercare", "care", "hello", "admin", "sales")

# What a 36-hour developer hackathon can actually offer a sponsor: access to
# student engineers, a product they might build on, a hiring funnel.
GOOD_WORDS = (
    "api", "sdk", "developer", "devtool", "dev tool", "cloud", "hosting",
    "database", "software", "platform", "saas", "infrastructure", "devops",
    "open source", "opensource", "ide", "compiler", "framework", "analytics",
    "machine learning", "artificial intelligence", "ai", "ml",
    "llm", "blockchain", "web3", "crypto", "fintech", "payments", "security",
    "identity", "auth", "testing", "monitoring", "semiconductor", "chip",
    "robotics", "iot", "edtech", "hackathon", "developer relations", "devrel",
    "it solutions", "software development", "product engineering",
)
# Audiences a hackathon cannot serve. These lists were built for a TEDx talk
# and a cultural fest; a diamond exporter has no use for 300 student coders.
BAD_WORDS = (
    "fmcg", "dairy", "ice cream", "pharma", "diamond", "gems", "textile",
    "saree", "ethnic", "fashion", "cement", "media", "television", "tv ",
    "news", "radio", "coaching", "cat ", "upsc", "civil services",
    "foreign education", "overseas", "visa", "immigration", "ayurvedic",
    "wellness", "eyewear", "restaurant", "food", "beverage", "cafe",
    "real estate", "hotel", "travel", "insurance", "jewel",
)
INDIA_WORDS = ("india", "bangalore", "bengaluru", "mumbai", "delhi", "pune",
               "hyderabad", "chennai", "gurgaon", "noida", "surat",
               "ahmedabad", "gujarat", "kolkata")


@dataclass
class Lead:
    company: str
    email: str = ""
    contact_name: str = ""
    title: str = ""
    phone: str = ""
    linkedin: str = ""
    notes: str = ""
    sources: set = field(default_factory=set)
    prior_contact: str = ""          # what an earlier sheet recorded
    emails: set = field(default_factory=set)

    @property
    def key(self) -> str:
        return norm_company(self.company)


# ---------------------------------------------------------------------------
# readers -- one per file shape
# ---------------------------------------------------------------------------

def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first_email(*values) -> str:
    for value in values:
        for found in EMAIL.findall(str(value or "")):
            return found.strip().lower()
    return ""


def _all_emails(*values) -> set:
    out = set()
    for value in values:
        out |= {e.lower() for e in EMAIL.findall(str(value or ""))}
    return out


def read_csv(path: Path) -> list[Lead]:
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    out: list[Lead] = []
    for row in rows:
        row = {(k or "").strip(): _clean(v) for k, v in row.items() if k}
        company = row.get("Company Name") or row.get("Company") or ""
        if not company:
            continue
        emails = _all_emails(row.get("Company Email"), row.get("Email"),
                             row.get("Contact Email"), row.get("Personal Email"))
        out.append(Lead(
            company=company.rstrip("\\"),
            email=_first_email(row.get("Company Email"), row.get("Contact Email"),
                               row.get("Email"), row.get("Personal Email")),
            emails=emails,
            contact_name=row.get("PoC Name") or row.get("POC Name")
            or row.get("Contact Name") or "",
            title=row.get("Position") or row.get("Contact Job Title") or "",
            phone=row.get("Contact Number") or row.get("Contact No.")
            or row.get("Contact #") or "",
            linkedin=row.get("LinkedIn") or "",
            notes=" ".join(x for x in (row.get("Notes"),
                                       row.get("Company Description"),
                                       row.get("Company Based out of")) if x),
            prior_contact=" ".join(x for x in (row.get("Status"),
                                               row.get("Last Contacted"),
                                               row.get("PrevEmails Info")) if x),
            sources={path.name}))
    return out


def read_xlsx(path: Path) -> list[Lead]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [_clean(c).lower() for c in rows[1]]
    idx = {name: header.index(name) for name in header if name}

    def cell(row, name):
        i = idx.get(name)
        return _clean(row[i]) if i is not None and i < len(row) else ""

    out: list[Lead] = []
    for row in rows[2:]:
        if not row or not _clean(row[0]):
            continue
        out.append(Lead(
            company=_clean(row[0]),
            email=_first_email(cell(row, "poc email")),
            emails=_all_emails(cell(row, "poc email")),
            contact_name=cell(row, "poc name"),
            title=cell(row, "poc designation"),
            phone=cell(row, "poc contact number"),
            linkedin=cell(row, "linkedin id"),
            notes=cell(row, "remark"),
            prior_contact=cell(row, "call remark"),
            sources={path.name}))
    return out


def read_pdf(path: Path) -> list[Lead]:
    """The PDFs are numbered or line-separated lists, not tables. Anchor on
    the email addresses and take the nearest preceding non-empty line as the
    company; anything cleverer guesses."""
    import fitz

    text = "\n".join(page.get_text() for page in fitz.open(path))
    lines = [_clean(ln) for ln in text.splitlines()]
    out: list[Lead] = []
    for i, line in enumerate(lines):
        found = EMAIL.findall(line)
        if not found:
            continue
        company = ""
        for back in range(i - 1, max(-1, i - 6), -1):
            candidate = lines[back]
            if not candidate or EMAIL.search(candidate):
                continue
            if re.fullmatch(r"[\d\s+()./-]+", candidate):     # a phone line
                continue
            if candidate.lower().startswith(("phone", "email", "brand", "tel")):
                continue
            company = candidate
            break
        company = re.sub(r"^\d+[.)]\s*", "", company)          # "1. Amul"
        note = ""
        match = re.search(r"\(([^)]+)\)\s*$", company)
        if match:
            note = match.group(1)
            company = company[:match.start()].strip()
        if company:
            out.append(Lead(company=company, email=found[0].lower(),
                            emails={e.lower() for e in found}, notes=note,
                            sources={path.name}))
    return out


def read_any(path: Path) -> list[Lead]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv(path)
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".pdf":
        return read_pdf(path)
    raise SystemExit("unsupported file: " + str(path))


# ---------------------------------------------------------------------------
# merge and score
# ---------------------------------------------------------------------------

def merge(leads: list[Lead]) -> list[Lead]:
    """One row per company, keeping the best address and every source."""
    merged: dict[str, Lead] = {}
    for lead in leads:
        if not lead.key:
            continue
        seen = merged.get(lead.key)
        if seen is None:
            merged[lead.key] = lead
            continue
        seen.sources |= lead.sources
        seen.emails |= lead.emails
        for attr in ("contact_name", "title", "phone", "linkedin"):
            if not getattr(seen, attr) and getattr(lead, attr):
                setattr(seen, attr, getattr(lead, attr))
        for attr in ("notes", "prior_contact"):
            extra = getattr(lead, attr)
            if extra and extra not in getattr(seen, attr):
                setattr(seen, attr, "; ".join(
                    x for x in (getattr(seen, attr), extra) if x))
        if len(lead.company) > len(seen.company):
            seen.company = lead.company
    for lead in merged.values():
        lead.email = _best_email(lead)
    # A single-word "company" that is just the contact's first name is a
    # parsing artefact, not a prospect.
    return [lead for lead in merged.values()
            if not (len(lead.company.split()) == 1 and lead.contact_name
                    and lead.company.lower() ==
                    lead.contact_name.split()[0].lower())]


def _best_email(lead: Lead) -> str:
    """A role mailbox on the company's own domain beats a personal one.

    Cold-mailing somebody's private Gmail from a script is the fastest way to
    be reported as spam, so a company address always wins even when the sheet
    listed the personal one first.
    """
    def rank(address: str) -> tuple:
        local, _, domain = address.partition("@")
        free = domain in FREE_MAIL
        generic = local.split(".")[0] in GENERIC_LOCAL
        return (free, generic, len(address))

    candidates = sorted(lead.emails or ({lead.email} if lead.email else set()),
                        key=rank)
    return candidates[0] if candidates else ""


def _has_word(haystack: str, words) -> bool:
    """Whole-word match. Substring matching put the National Highways
    Authority in the top band because "nhai" contains "ai"."""
    return any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", haystack)
               for word in words)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def domain_mismatch(lead: Lead) -> bool:
    """Does the address look like it belongs to somebody else?

    The single worst thing this list can do is put a real address next to the
    wrong company: the ancestor project's live sweep surfaced another
    charity's sponsorship inbox for Groq. Legitimate aliases exist (AWS uses
    amazon.com), so this flags rather than deletes -- the human reads the top
    band anyway.
    """
    if not lead.email:
        return False
    domain = lead.email.partition("@")[2]
    if domain in FREE_MAIL:
        return False
    company = _slug(lead.company)
    if not company:
        return False
    labels = [_slug(part) for part in domain.split(".") if len(part) > 2]
    for label in labels:
        if not label:
            continue
        if label in company or company in label:
            return False                        # the domain is plausibly theirs
        if company[:6] and label.startswith(company[:6]):
            return False
        if label[:6] and company.startswith(label[:6]):
            return False
    return True


def score(lead: Lead, past_sponsors: set) -> tuple[int, list[str]]:
    """Fit for a 36-hour developer hackathon, and why."""
    points, why = 0, []
    haystack = " ".join((lead.company, lead.notes, lead.title)).lower()

    if lead.key in past_sponsors:
        points += 60
        why.append("sponsored a past DotSlash")
    if _has_word(haystack, GOOD_WORDS):
        points += 25
        why.append("tech/developer audience")
    if _has_word(haystack, BAD_WORDS):
        points -= 35
        why.append("audience does not fit a hackathon")
    if _has_word(haystack, INDIA_WORDS):
        points += 12
        why.append("India presence")

    if lead.email:
        local, _, domain = lead.email.partition("@")
        if domain in FREE_MAIL:
            points -= 25
            why.append("only a personal address")
        elif local.split(".")[0] in GENERIC_LOCAL:
            points -= 12
            why.append("generic inbox")
        else:
            points += 20
            why.append("company address")
        if lead.contact_name and domain not in FREE_MAIL:
            points += 15
            why.append("named contact")
        if domain_mismatch(lead):
            points -= 30
            why.append("ADDRESS MAY BE ANOTHER COMPANY'S")
    else:
        points -= 20
        why.append("no address yet")

    if "MLH" in " ".join(lead.sources):
        points += 12
        why.append("from the hackathon list")
    if "hackathon sponsorship" in " ".join(lead.sources):
        points += 18
        why.append("curated tech contact")
    # "Mailed" / "approached" in these sheets is LAST YEAR'S outreach, for a
    # previous edition. That is not a reason to skip them now -- a new edition
    # is a new ask, and nobody has been written to this year. Only a recorded
    # *response* changes the ranking, and it changes it upward.
    if re.search(r"replied|reply|talks|stage|interested|mentor|knows|warm",
                 lead.prior_contact, re.I):
        points += 20
        why.append("answered us before")
    elif re.search(r"mailed|approached", lead.prior_contact, re.I):
        why.append("written to for a past edition")
    if len(lead.sources) > 1:
        points += 5
        why.append("on {} lists".format(len(lead.sources)))
    return points, why


def band(points: int) -> str:
    if points >= 60:
        return "1-warm"
    if points >= 40:
        return "2-strong"
    if points >= 20:
        return "3-plausible"
    return "4-long-shot"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--out", default=str(DATA / "prospects.ranked.csv"))
    parser.add_argument("--top", type=int, default=25, help="how many to print")
    parser.add_argument("--apply", action="store_true",
                        help="import into the database (still mails nothing)")
    parser.add_argument("--bands", default="1,2",
                        help="which bands --apply imports (default the warm and "
                             "strong ones)")
    args = parser.parse_args(argv)

    event = Event.load(DATA / "event.yml")
    past = {norm_company(name) for name in event.past_sponsors}

    leads: list[Lead] = []
    for name in args.files:
        path = Path(name)
        if not path.exists():
            print("! missing:", path)
            continue
        found = read_any(path)
        print("{:<46} {:>5} row(s)".format(path.name[:46], len(found)))
        leads += found

    merged = merge(leads)
    scored = []
    for lead in merged:
        points, why = score(lead, past)
        scored.append((points, band(points), lead, why))
    scored.sort(key=lambda row: (-row[0], row[2].company.lower()))

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["band", "score", "company", "email", "contact_name",
                         "title", "phone", "linkedin", "check_domain", "why",
                         "prior_contact", "notes", "sources"])
        for points, tier, lead, why in scored:
            writer.writerow([tier, points, lead.company, lead.email,
                             lead.contact_name, lead.title, lead.phone,
                             lead.linkedin,
                             "CHECK" if domain_mismatch(lead) else "",
                             "; ".join(why), lead.prior_contact, lead.notes,
                             ", ".join(sorted(lead.sources))])

    counts: dict = {}
    for _points, tier, lead, _why in scored:
        counts[tier] = counts.get(tier, 0) + 1
    print("\n{} companies after dedupe ({} raw rows)".format(len(scored), len(leads)))
    for tier in sorted(counts):
        print("  {:<14} {}".format(tier, counts[tier]))
    print("\ntop {}:".format(args.top))
    print("  {:<4} {:<26} {:<34} {}".format("pts", "company", "email", "why"))
    for points, tier, lead, why in scored[:args.top]:
        print("  {:<4} {:<26} {:<34} {}".format(
            points, lead.company[:26], (lead.email or "-")[:34],
            "; ".join(why)[:60]))
    print("\nwrote", out)

    if not args.apply:
        print("nothing written to the database. Re-run with --apply to import "
              "band(s) {}.".format(args.bands))
        return 0

    from sponsy import store                       # noqa: PLC0415
    from sponsy.state import Database              # noqa: PLC0415

    wanted = {b.strip() for b in args.bands.split(",") if b.strip()}
    db = Database(DATA / "sponsy.db")
    store.ensure_schema(db)
    added = updated = 0
    try:
        for points, tier, lead, why in scored:
            if tier.split("-")[0] not in wanted:
                continue
            existing = store.find_by_company(db, lead.company)
            if existing is not None:
                # Re-running the ingest should refresh the ranking, not skip
                # everything it already knows. State (status, touches) is
                # never touched -- only the running order and the context.
                db.execute("UPDATE prospects SET priority=? WHERE id=?",
                           (max(1, 200 - points), existing.id))
                if lead.email and not existing.email:
                    store.set_email(db, existing.id, lead.email,
                                    source_url="ranked-list")
                updated += 1
                continue
            note = "; ".join(x for x in (
                "band {} ({} pts): {}".format(tier, points, "; ".join(why)),
                lead.title,
                "prior contact: " + lead.prior_contact if lead.prior_contact else "",
                lead.linkedin, lead.notes) if x)[:900]
            store.add_prospect(db, lead.company, email=lead.email,
                               contact_name=lead.contact_name, notes=note,
                               source="ranked-list",
                               # lower number = worked first
                               priority=max(1, 200 - points))
            added += 1
    finally:
        db.close()
    print("\nimported {} new prospect(s) into {}; refreshed {} already there"
          .format(added, DATA / "sponsy.db", updated))
    print("Nothing has been mailed. Each one still needs a run, a card and a tap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
