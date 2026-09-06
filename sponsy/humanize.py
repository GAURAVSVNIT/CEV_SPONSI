"""AI-tell lint and the one rewrite pass.

Layer two and three of the humanizer (layer one is the voice block in the
drafting prompt). The lint is deterministic and free: it looks for the things
that make a reader think "a bot wrote this" -- stock openers, em-dashes,
exclamation marks, bullet points, sentences that all start with "I", a hundred
words without a single contraction, sentences that are all the same length, a
subject line that reads like a campaign. It produces a 0..1 score and a list of
issues written as instructions, because the list is what the rewrite pass and
the drafter's retry are fed.

The rewrite pass is one model call and it is not trusted: the result is only
accepted if it kept every number, did not grow, and is not empty. Otherwise the
original draft goes forward and the rejection is logged.
"""

from __future__ import annotations

import dataclasses
import re
import statistics
from dataclasses import dataclass, field

PASS_SCORE = 0.7

# Lowercase. Matched whole-word, case-insensitive, after normalising curly
# apostrophes. Overlapping hits ("i'm reaching out" inside "reaching out to")
# count once, and the total deduction is capped so one bad paragraph does not
# zero the score on its own.
BANNED_PHRASES: tuple[str, ...] = (
    "i hope this email finds you well",
    "hope this finds you well",
    "i hope you are doing well",
    "i hope you're doing well",
    "trust this email finds you",
    "i'm reaching out",
    "i am reaching out",
    "reaching out to",
    "i wanted to reach out",
    "i wanted to",
    "excited",
    "thrilled",
    "delighted",
    "passionate about",
    "leverage",
    "synergy",
    "synergies",
    "mutually beneficial",
    "esteemed",
    "innovative",
    "cutting-edge",
    "seamless",
    "seamlessly",
    "unlock",
    "elevate",
    "delve",
    "in today's fast-paced",
    "feel free to",
    "don't hesitate",
    "do not hesitate",
    "at your earliest convenience",
    "looking forward to hearing from you",
    "i hope this helps",
    "game-changer",
    "game changing",
    "world-class",
    "best-in-class",
    "revolutionary",
    "unparalleled",
    "empower",
    "robust",
    "tailored",
    "top-notch",
    "as an ai",
    "win-win",
    "perfect fit",
    "invaluable",
)

SALESY_SUBJECT = re.compile(
    r"partnership opportunity|collaboration proposal|sponsorship request"
    r"|sponsorship opportunity|exciting|opportunity to|[:|]", re.I)

# Language that pushes. The emails are a request from a student club to a
# company that owes it nothing, so anything that manufactures urgency or
# implies the recipient has no choice is a defect, not a style preference --
# `verify` treats a hit here as a hard failure.
PRESSURE_PHRASES: tuple[str, ...] = (
    "limited slots", "limited spots", "limited seats", "limited time",
    "limited availability", "only a few", "act now", "act fast", "hurry",
    "last chance", "final call", "don't miss", "do not miss", "miss out",
    "you must", "you need to", "you have to", "we require", "we expect you",
    "secure your spot", "secure your place", "reserve your spot",
    "offer expires", "expires soon", "closing soon", "deadline is approaching",
    "before it is too late", "before it's too late", "urgent", "urgently",
    "exclusive opportunity", "once in a lifetime", "the only tier",
    "this tier only", "no other option", "immediate action",
)

BANNED_CAP = 0.5
EXCLAMATION_CAP = 0.3
PRESSURE_CAP = 0.6
CONTRACTION_MIN_WORDS = 60
UNIFORM_MIN_SENTENCES = 5
UNIFORM_STDEV = 3.0
SUBJECT_MAX = 70
# A proposal may list what a sponsor gets. More than this and the email has
# stopped being an email and become the brochure it should have attached.
MAX_LIST_LINES = 4
# Sentences opening with "I" only read as robotic when most of them do; a
# count alone punishes any email long enough to say something.
I_SENTENCE_MIN = 3
I_SENTENCE_SHARE = 0.4

# A real em-dash, a double hyphen, or a spaced hyphen/en-dash between words.
EM_DASH = re.compile(r"—|--|(?<=\w) [-–] (?=\w)")
# A line that is a bullet, a heading, or bold text; two numbered lines are a
# list too. Any of these means the model formatted instead of writing.
MARKDOWN_LINE = re.compile(r"^\s*[-*+•·‣▪]\s+")
HEADING_LINE = re.compile(r"^\s*#{1,6}\s+")
NUMBERED_LINE = re.compile(r"^\s*\d+[.)]\s+")
BOLD = re.compile(r"\*\*")
GREETING_LINE = re.compile(
    r"^\s*(?:hi|hello|dear|hey|good (?:morning|afternoon|evening))\b", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
I_SENTENCE = re.compile(r"I(?:\s|'|’)")
# Possessive "'s" is not a contraction, so only the pronoun/adverb forms count
# for it; every n't / 're / 've / 'll / 'd / 'm does.
CONTRACTION = re.compile(
    r"\b(?:\w+n't|\w+'(?:re|ve|ll|d|m)"
    r"|(?:it|that|there|here|what|let|he|she|who|where|how|when|why)'s)\b", re.I)
PLACEHOLDER = re.compile(
    r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}|<insert|\bTODO\b|\bLorem\b", re.I)
GENERIC_SALUTATION = re.compile(
    r"dear\s+sir\s*(?:/|or)\s*madam|to\s+whom\s+it\s+may\s+concern", re.I)


@dataclass
class LintResult:
    score: float
    issues: list[str] = field(default_factory=list)
    flags: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.score >= PASS_SCORE

    def to_dict(self) -> dict:
        return {"score": self.score, "passed": self.passed,
                "issues": list(self.issues), "flags": dict(self.flags)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Curly apostrophes and quotes become straight ones so "don’t" and
    "don't" are the same word to every regex below."""
    return (str(text or "").replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"'))


def _find_phrases(text: str, phrases) -> list[str]:
    """Which of `phrases` occur whole-word in `text`? Longest match wins where
    two phrases cover the same span, so "I'm reaching out to" is reported once."""
    low = _norm(text).lower()
    hits: list[tuple[int, int, str]] = []
    for phrase in phrases:
        phrase = _norm(phrase).strip().lower()
        if not phrase:
            continue
        pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
        for match in re.finditer(pattern, low):
            hits.append((match.start(), match.end(), phrase))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    kept: list[tuple[int, int, str]] = []
    for start, end, phrase in hits:
        if any(start < k_end and end > k_start for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, phrase))
    kept.sort()
    out: list[str] = []
    for _, _, phrase in kept:
        if phrase not in out:
            out.append(phrase)
    return out


def _body_lines(body: str, voice=None) -> list[str]:
    """The lines that carry prose: no greeting, no sign-off block."""
    lines = [ln.rstrip() for ln in _norm(body).split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if lines and GREETING_LINE.match(lines[0]) and len(lines[0].split()) <= 6:
        lines = lines[1:]
    signoff_first = _signoff_first_line(voice)
    if signoff_first:
        for i in range(max(0, len(lines) - 4), len(lines)):
            if signoff_first in lines[i].lower():
                lines = lines[:i]
                break
    # Whatever is left at the tail that is short and unpunctuated is a name
    # line, a role line, or a "Thanks," -- not a sentence.
    popped = 0
    while lines and popped < 5 and len(lines[-1].split()) <= 6 \
            and not lines[-1].rstrip().endswith((".", "!", "?")):
        lines.pop()
        popped += 1
    return lines


def _sentences(body: str, voice=None) -> list[str]:
    text = " ".join(_body_lines(body, voice))
    return [s.strip() for s in SENTENCE_END.split(text) if s.strip()]


def _signoff_first_line(voice) -> str:
    signoff = str(getattr(voice, "signoff", "") or "")
    for line in signoff.split("\n"):
        if line.strip():
            return _norm(line).strip().lower()
    return ""


def _signoff_present(body: str, voice) -> bool:
    first = _signoff_first_line(voice)
    if not first:
        return True
    tail = [ln for ln in _norm(body).split("\n") if ln.strip()][-4:]
    return any(first in ln.lower() for ln in tail)


# ---------------------------------------------------------------------------
# the lint
# ---------------------------------------------------------------------------

def lint(body: str, subject: str = "", voice=None,
         signoff_expected: bool = False) -> LintResult:
    """Score 1.0 is clean. Every deduction adds one sentence to `issues` that
    tells the writer what to change, not just what is wrong."""
    body = str(body or "")
    subject = str(subject or "")
    if not body.strip() and not subject.strip():
        return LintResult(score=1.0, issues=[], flags={"words": 0})

    issues: list[str] = []
    flags: dict = {}
    total = 0.0
    norm_body = _norm(body)
    words = len(norm_body.split())
    flags["words"] = words

    # stock phrases
    banned = _find_phrases(norm_body, BANNED_PHRASES)
    flags["banned"] = banned
    if banned:
        total += min(BANNED_CAP, 0.15 * len(banned))
        for phrase in banned:
            issues.append('Remove "{}"; it is stock phrasing nobody types, say it '
                          'plainly or cut it.'.format(phrase))

    avoided = _find_phrases(norm_body, getattr(voice, "phrases_i_avoid", None) or [])
    avoided = [p for p in avoided if p not in banned]
    flags["avoided"] = avoided
    if avoided:
        total += 0.15 * len(avoided)
        for phrase in avoided:
            issues.append('Remove "{}"; the sender never writes that.'.format(phrase))

    # punctuation tells
    dashes = len(EM_DASH.findall(norm_body))
    flags["em_dashes"] = dashes
    if dashes > 1:
        total += 0.15
        issues.append("There are {} dashes used as em-dashes; keep at most one and "
                      "use commas or full stops instead.".format(dashes))

    bangs = norm_body.count("!") + subject.count("!")
    flags["exclamations"] = bangs
    if bangs:
        total += min(EXCLAMATION_CAP, 0.1 * bangs)
        issues.append("Drop the {} exclamation mark{}; end sentences with full "
                      "stops.".format(bangs, "" if bangs == 1 else "s"))

    # pressure
    pressure = _find_phrases(norm_body, PRESSURE_PHRASES)
    flags["pressure"] = pressure
    if pressure:
        total += min(PRESSURE_CAP, 0.2 * len(pressure))
        for phrase in pressure:
            issues.append('Delete "{}"; we are asking a company for a favour, not '
                          'selling to them. No urgency, no obligation.'.format(phrase))

    # formatting instead of writing. A short list of what a sponsor gets is
    # legitimate in a proposal; a wall of them means the brochure got pasted in.
    lines = norm_body.split("\n")
    listed = sum(1 for ln in lines
                 if MARKDOWN_LINE.match(ln) or NUMBERED_LINE.match(ln))
    heading_or_bold = (bool(BOLD.search(norm_body))
                       or any(HEADING_LINE.match(ln) for ln in lines))
    flags["list_lines"] = listed
    markdown = heading_or_bold or listed > MAX_LIST_LINES
    flags["markdown"] = markdown
    if heading_or_bold:
        total += 0.2
        issues.append("Remove the markdown headings and bold; an email is plain "
                      "text.")
    elif listed > MAX_LIST_LINES:
        total += 0.2
        issues.append("There are {} list lines; keep at most {} and put the rest "
                      "in the attached brochure.".format(listed, MAX_LIST_LINES))

    # rhythm
    sentences = _sentences(norm_body, voice)
    lengths = [len(s.split()) for s in sentences]
    flags["sentences"] = len(sentences)
    i_count = sum(1 for s in sentences if I_SENTENCE.match(s))
    flags["i_sentences"] = i_count
    if i_count >= I_SENTENCE_MIN and sentences and \
            i_count / len(sentences) >= I_SENTENCE_SHARE:
        total += 0.1
        issues.append('{} of {} sentences start with "I"; recast some of them so '
                      'they start with the reader or with the point.'.format(
                          i_count, len(sentences)))

    no_contractions = words >= CONTRACTION_MIN_WORDS and not CONTRACTION.search(norm_body)
    flags["no_contractions"] = no_contractions
    if no_contractions:
        total += 0.1
        issues.append("There is not one contraction in {} words; use a few (we're, "
                      "I'd, it's). Nobody writes 100 words without a single "
                      "contraction.".format(words))

    uniform = False
    if len(lengths) >= UNIFORM_MIN_SENTENCES:
        stdev = statistics.pstdev(lengths)
        flags["sentence_stdev"] = round(stdev, 2)
        uniform = stdev < UNIFORM_STDEV
    flags["uniform_sentences"] = uniform
    if uniform:
        total += 0.1
        issues.append("All {} sentences are about {} words long; vary the rhythm, "
                      "mix a short sentence in with a longer one.".format(
                          len(lengths), round(statistics.mean(lengths))))

    # subject
    salesy = SALESY_SUBJECT.search(subject)
    flags["salesy_subject"] = salesy.group(0) if salesy else ""
    if salesy:
        total += 0.15
        issues.append('The subject reads like a campaign ("{}"); make it quiet and '
                      'specific, no colons or pipes, no "opportunity".'.format(
                          salesy.group(0)))
    flags["subject_long"] = len(subject) > SUBJECT_MAX
    if len(subject) > SUBJECT_MAX:
        total += 0.05
        issues.append("The subject is {} characters; keep it under {}.".format(
            len(subject), SUBJECT_MAX))

    # closing. The HTML template supplies the sign-off and the contact block,
    # so by default the model's prose must NOT carry one -- a second copy
    # renders as a doubled signature. `signoff_expected=True` flips this back
    # for anything that sends the prose as the whole email.
    present = _signoff_present(norm_body, voice) if voice is not None else None
    if present is None:
        flags["signoff_mismatch"] = False
    elif signoff_expected:
        flags["signoff_mismatch"] = not present
        if not present:
            total += 0.1
            issues.append('Close with "{}" the way the sender does, not with the '
                          'ending that is there now.'.format(
                              _signoff_first_line(voice)))
    else:
        flags["signoff_mismatch"] = present
        if present:
            total += 0.1
            issues.append('Delete the sign-off and the contact details; they are '
                          'added after your text and would appear twice.')

    # form-letter leftovers
    placeholder = PLACEHOLDER.search(norm_body)
    flags["placeholder"] = placeholder.group(0) if placeholder else ""
    if placeholder:
        total += 0.2
        issues.append("There is placeholder text ({}) in the body; replace it with "
                      "the real detail or cut the sentence.".format(
                          placeholder.group(0)))

    generic = GENERIC_SALUTATION.search(norm_body)
    flags["generic_salutation"] = generic.group(0) if generic else ""
    if generic:
        total += 0.15
        issues.append('Replace "{}" with the greeting the sender uses; this is a '
                      'note to one person, not a form letter.'.format(generic.group(0)))

    score = round(max(0.0, 1.0 - total), 2)
    return LintResult(score=score, issues=issues, flags=flags)


# ---------------------------------------------------------------------------
# the rewrite pass
# ---------------------------------------------------------------------------

def _tidy(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n")
    text = re.sub(r"^subject:.*\n+", "", text.strip(), flags=re.I)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def humanize_pass(llm, cfg, db, draft, voice, issues, run_id=None):
    """One editor call, then a hard check on what came back.

    The model is told to fix exactly `issues` and nothing else. We cannot verify
    "nothing else" in general, but we can verify the two cheap proxies that
    matter most: every number the original had is still there and no new one
    appeared (an editor that changes Rs 20,000 to Rs 25,000 is worse than no
    editor), and the text did not grow. Claims are never touched here; the
    verifier re-grounds them against the new body anyway.
    """
    from .agent import structured  # noqa: PLC0415
    from .verify import _numbers  # noqa: PLC0415  (verify imports this module)
    from . import prompts  # noqa: PLC0415

    issue_lines = "\n".join("- " + str(i) for i in (issues or [])) or "- (none listed)"
    user = prompts.HUMANIZE_USER.format(
        voice_block=voice.style_block(), examples_block=voice.examples_block(),
        issues=issue_lines, subject=draft.subject, body=draft.body)
    data, cost = structured(llm, system=prompts.HUMANIZE_SYSTEM, user=user,
                            temperature=0.4, db=db, run_id=run_id,
                            kind="humanize", target=draft.prospect_id)

    new_body = _tidy(data.get("body"))
    new_subject = (str(data.get("subject") or "").strip() or draft.subject)[:120]

    reason = ""
    if not new_body or len(new_body.split()) < 3:
        reason = "rewrite came back empty"
    else:
        before, after = _numbers(draft.body), _numbers(new_body)
        if before - after:
            reason = "rewrite dropped number(s): " + ", ".join(sorted(before - after))
        elif after - before:
            reason = "rewrite added number(s): " + ", ".join(sorted(after - before))
        else:
            old_words, words = len(draft.body.split()), len(new_body.split())
            if words > old_words * 1.10:
                reason = "rewrite grew from {} to {} words".format(old_words, words)

    if reason:
        if db is not None:
            db.log_action(run_id, "humanize_rejected", draft.prospect_id,
                          {"reason": reason, "subject": new_subject,
                           "body": new_body[:600]}, confidence=0.0)
        return draft, cost

    rewritten = dataclasses.replace(draft, subject=new_subject, body=new_body,
                                    claims=list(draft.claims), humanized=True)
    return rewritten, cost
