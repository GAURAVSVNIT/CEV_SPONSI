"""Environment, event and voice configuration.

Dependency-light on purpose: a 20-line .env parser instead of python-dotenv,
and PyYAML only for the two data files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import norm_company

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# USD per million tokens (input, output). Estimates -- check current pricing
# before quoting a cost figure anywhere. Unknown models fall back to DEFAULT.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "fake": (0.0, 0.0),
}
DEFAULT_PRICE = (0.30, 2.50)

# (default model, cheap model) per provider. The cheap one handles reply
# classification and the groundedness judge; the default one does research and
# drafting.
DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini-3.5-flash", "gemini-3.5-flash-lite"),
    "openai": ("gpt-5-mini", "gpt-5-nano"),
    "anthropic": ("claude-opus-5", "claude-haiku-4-5"),
    "fake": ("fake", "fake"),
}

LLM_PROVIDERS = ("auto", "gemini", "openai", "anthropic", "fake")
SEARCH_PROVIDERS = ("auto", "tavily", "brave", "ddg", "none")
MAIL_PROVIDERS = ("auto", "gmail_api", "smtp", "manual")
REPLIES_PROVIDERS = ("auto", "gmail_api", "imap", "none")


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing env vars always win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if val[:1] == val[-1:] and val[:1] in {'"', "'"} and len(val) >= 2:
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key)
    if not val:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_path(key: str, default: Path) -> Path:
    val = _env(key)
    if not val:
        return default
    path = Path(val)
    return path if path.is_absolute() else ROOT / path


@dataclass
class Config:
    # --- model ---
    llm_provider: str = "auto"            # auto | gemini | openai | anthropic | fake
    gemini_api_key: str = ""
    gemini_api_keys: list[str] = field(default_factory=list)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_fallbacks: bool = True      # server-side refusal fallbacks on claude-opus-5
    model: str = ""                       # blank = provider default
    model_cheap: str = ""
    thinking_budget: int = 0              # Gemini only; 0 = off
    max_steps: int = 8                    # per prospect research loop
    max_cost_usd: float = 0.05
    min_interval_s: float = 6.5           # Gemini free-tier pacing; 0 once billed

    # --- telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- research ---
    search_provider: str = "auto"         # auto | tavily | brave | ddg | none
    tavily_api_key: str = ""
    brave_api_key: str = ""
    max_fetches: int = 4                  # per prospect, per run. Hard cap.
    fetch_timeout: int = 15
    user_agent: str = (
        "sponsy/0.1 (student club sponsorship research; "
        "contact via the address in the outreach email)"
    )

    # --- quality ---
    grounding_mode: str = "hybrid"        # hybrid | keyword | model | off
    humanize: bool = True
    humanize_strict: bool = False

    # --- outbound mail ---
    mail_provider: str = "auto"           # auto | gmail_api | smtp | manual
    gmail_credentials: Path = field(default_factory=lambda: DATA / "gmail_credentials.json")
    gmail_token: Path = field(default_factory=lambda: DATA / "gmail_token.json")
    gmail_sender: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    allow_send: bool = True
    # Test-phase safety valve. While this is non-empty, a [Send] to any other
    # address writes the .eml and stops there. It exists because the dangerous
    # moment is the one where mail works for the first time and a real
    # prospect's card is still sitting in the chat one tap away.
    send_only_to: list[str] = field(default_factory=list)
    # The sponsorship brochure, attached to the first email and to any reply
    # that answers a question. Attach nothing by pointing this at a file that
    # does not exist (or deleting it): a missing brochure is never an error,
    # it just means a plain-text email.
    brochure: Path = field(default_factory=lambda: DATA / "brochure.pdf")

    # --- replies ---
    replies_provider: str = "auto"        # auto | gmail_api | imap | none
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    # --- runtime ---
    db_path: Path = field(default_factory=lambda: DATA / "sponsy.db")
    outbox: Path = field(default_factory=lambda: DATA / "outbox")
    event_path: Path = field(default_factory=lambda: DATA / "event.yml")
    voice_path: Path = field(default_factory=lambda: DATA / "voice.yml")
    dry_run: bool = False
    offline: bool = False

    @classmethod
    def load(cls, **overrides: Any) -> "Config":
        load_dotenv()
        cfg = cls(
            llm_provider=_env("LLM_PROVIDER", "auto").lower(),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_api_keys=[k.strip() for k in _env("GEMINI_API_KEYS").split(",")
                             if k.strip()],
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_fallbacks=_env_bool("ANTHROPIC_FALLBACKS", True),
            model=_env("LLM_MODEL"),
            model_cheap=_env("LLM_MODEL_CHEAP"),
            thinking_budget=_env_int("LLM_THINKING_BUDGET", 0),
            max_steps=_env_int("LLM_MAX_STEPS", 8),
            max_cost_usd=_env_float("LLM_MAX_COST", 0.05),
            min_interval_s=_env_float("LLM_MIN_INTERVAL", 6.5),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            search_provider=_env("SEARCH_PROVIDER", "auto").lower(),
            tavily_api_key=_env("TAVILY_API_KEY"),
            brave_api_key=_env("BRAVE_API_KEY"),
            max_fetches=_env_int("MAX_FETCHES", 4),
            grounding_mode=_env("GROUNDING", "hybrid").lower(),
            humanize=_env_bool("HUMANIZE", True),
            humanize_strict=_env_bool("HUMANIZE_STRICT", False),
            mail_provider=_env("MAIL_PROVIDER", "auto").lower(),
            gmail_credentials=_env_path("GMAIL_CREDENTIALS", DATA / "gmail_credentials.json"),
            gmail_token=_env_path("GMAIL_TOKEN", DATA / "gmail_token.json"),
            gmail_sender=_env("GMAIL_SENDER"),
            smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM"),
            allow_send=_env_bool("ALLOW_SEND", True),
            send_only_to=[a.strip().lower() for a in _env("SEND_ONLY_TO").split(",")
                          if a.strip()],
            brochure=_env_path("BROCHURE", DATA / "brochure.pdf"),
            replies_provider=_env("REPLIES_PROVIDER", "auto").lower(),
            imap_host=_env("IMAP_HOST", "imap.gmail.com"),
            imap_user=_env("IMAP_USER"),
            imap_password=_env("IMAP_PASSWORD"),
            imap_folder=_env("IMAP_FOLDER", "INBOX"),
            db_path=_env_path("SPONSY_DB", DATA / "sponsy.db"),
            event_path=_env_path("SPONSY_EVENT", DATA / "event.yml"),
            voice_path=_env_path("SPONSY_VOICE", DATA / "voice.yml"),
        )
        for key, val in overrides.items():
            if val is not None:
                setattr(cfg, key, val)
        if cfg.offline:
            # No network: fake model, no search, no mail, no reply polling, and
            # no Telegram -- a demo on frozen fixtures must not push a real
            # message to a real phone because a token happens to be in .env.
            cfg.llm_provider = "fake"
            cfg.search_provider = "none"
            cfg.mail_provider = "manual"
            cfg.replies_provider = "none"
        for name, allowed in (("llm_provider", LLM_PROVIDERS),
                              ("search_provider", SEARCH_PROVIDERS),
                              ("mail_provider", MAIL_PROVIDERS),
                              ("replies_provider", REPLIES_PROVIDERS)):
            if getattr(cfg, name) not in allowed:
                raise ValueError("{}={!r} is not one of {}".format(
                    name.upper(), getattr(cfg, name), ", ".join(allowed)))
        for attr in ("db_path", "outbox", "event_path", "voice_path",
                     "gmail_credentials", "gmail_token", "brochure"):
            setattr(cfg, attr, Path(getattr(cfg, attr)))
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.outbox.mkdir(parents=True, exist_ok=True)
        return cfg

    # --- derived: model ---------------------------------------------------
    @property
    def api_keys(self) -> list[str]:
        """Every configured Gemini key, in order, deduplicated."""
        out: list[str] = []
        for key in [self.gemini_api_key] + list(self.gemini_api_keys):
            key = (key or "").strip()
            if key and key not in out:
                out.append(key)
        return out

    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.api_keys:
            return "gemini"
        if self.openai_api_key:
            return "openai"
        if self.anthropic_api_key:
            return "anthropic"
        return "fake"

    def resolved_models(self) -> tuple[str, str]:
        default, cheap = DEFAULT_MODELS[self.resolved_llm_provider()]
        return (self.model or default, self.model_cheap or cheap)

    def price(self, model: str) -> tuple[float, float]:
        # longest prefix wins, so "gpt-5-mini" beats "gpt-5"
        best = ""
        for name in PRICES_USD_PER_MTOK:
            if model.startswith(name) and len(name) > len(best):
                best = name
        return PRICES_USD_PER_MTOK[best] if best else DEFAULT_PRICE

    # --- derived: research ------------------------------------------------
    def resolved_search_provider(self) -> str:
        if self.search_provider != "auto":
            return self.search_provider
        if self.tavily_api_key:
            return "tavily"
        if self.brave_api_key:
            return "brave"
        return "ddg"

    # --- derived: channels ------------------------------------------------
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def smtp_enabled(self) -> bool:
        # The password matters: without it we would announce SMTP, fail to log
        # in, and drop the email half way through an approval.
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def may_send_to(self, address: str) -> bool:
        """False when SEND_ONLY_TO is set and this is not one of them."""
        if not self.send_only_to:
            return True
        return (address or "").strip().lower() in self.send_only_to

    @property
    def brochure_file(self) -> Path | None:
        """The brochure, if there is one on disk. None is a normal state."""
        path = Path(self.brochure) if self.brochure else None
        return path if path and path.is_file() else None

    @property
    def gmail_api_enabled(self) -> bool:
        return Path(self.gmail_token).exists() or Path(self.gmail_credentials).exists()

    def resolved_mail_provider(self) -> str:
        """Which channel [Send] uses. `manual` means .eml + mailto, nothing leaves."""
        if not self.allow_send:
            return "manual"
        if self.mail_provider != "auto":
            return self.mail_provider
        if self.gmail_api_enabled:
            return "gmail_api"
        if self.smtp_enabled:
            return "smtp"
        return "manual"

    def resolved_replies_provider(self) -> str:
        if self.replies_provider != "auto":
            return self.replies_provider
        if self.mail_provider in ("auto", "gmail_api") and Path(self.gmail_token).exists():
            return "gmail_api"
        if self.imap_enabled:
            return "imap"
        return "none"


# ---------------------------------------------------------------------------
# Event: everything the drafter may say about US. Edit data/event.yml.
# ---------------------------------------------------------------------------

@dataclass
class Tier:
    name: str
    amount: int
    deliverables: list[str]
    currency: str = "USD"

    def line(self) -> str:
        return "{} ({} {:,}): {}".format(self.name, self.currency, self.amount,
                                         "; ".join(self.deliverables))


@dataclass
class Event:
    event_name: str
    date: str
    colleges: str
    tiers: list[Tier]
    sender_name: str
    sender_role: str
    club: str
    reply_to: str
    summary: str = ""
    currency: str = "USD"
    city: str = ""
    region: str = ""
    country: str = ""
    kind: str = "hackathon"
    expected_participants: int = 0
    prize_pool: int = 0
    phone: str = ""
    website: str = ""
    # Verified facts about past editions. The drafter may cite these; anything
    # not in this list or in the research is an ungrounded claim.
    track_record: list[str] = field(default_factory=list)
    registrations_so_far: int = 0
    confirmed_sponsors: list[str] = field(default_factory=list)

    # --- how the HTML email presents us -----------------------------------
    # Rendered into the message verbatim, so every line is asserted as fact
    # exactly like track_record. Each one is optional: an empty list or string
    # simply drops that block out of the email.
    theme: str = ""
    tagline: str = ""
    stats: list[dict] = field(default_factory=list)      # {value, label}
    stats_note: str = ""
    why_partner: list[str] = field(default_factory=list)
    what_we_offer: list[dict] = field(default_factory=list)   # {title, detail}
    custom_packages: str = ""
    legacy: str = ""
    # What is genuinely new this edition. Only the returning-sponsor email
    # uses it, and only if it is set: "we are doing it again" is a weak ask,
    # but an invented change is worse, so blank means the drafter is told to
    # lean on the edition number instead of inventing one.
    whats_new: str = ""
    past_sponsors: list[str] = field(default_factory=list)
    past_sponsors_logo: str = ""
    organisers: list[dict] = field(default_factory=list)   # {name, logo?}
    brand_logo: str = ""
    instagram: str = ""
    address: str = ""
    # Set by Event.load: where the files named in `organisers[].logo` live.
    logo_dir: Path | None = None
    team_contacts: list[dict] = field(default_factory=list)   # {name, phone}

    def track_record_block(self) -> str:
        if not self.track_record:
            return "(none recorded)"
        return "\n".join("- " + t for t in self.track_record)

    @property
    def tier_names(self) -> list[str]:
        return [t.name for t in self.tiers]

    def tier_block(self) -> str:
        return "\n".join("- " + t.line() for t in self.tiers)

    def where(self) -> str:
        return ", ".join(p for p in (self.city, self.region, self.country) if p)

    def is_past_sponsor(self, company: str) -> bool:
        """Did this company back a previous edition?

        Compared on the normalised name, so "Digital Ocean" in the prospect
        list matches "DigitalOcean" in past_sponsors. Getting this wrong in
        either direction writes the wrong email: a cold pitch to a returning
        sponsor looks forgetful, and a "welcome back" to a stranger is worse.
        """
        key = norm_company(company)
        if not key:
            return False
        return any(norm_company(name) == key for name in self.past_sponsors)

    @classmethod
    def load(cls, path: Path | None = None) -> "Event":
        import yaml  # noqa: PLC0415

        path = Path(path or DATA / "event.yml")
        if not path.exists():
            raise FileNotFoundError(
                "{} is missing. Copy data/event.example.yml to it and fill it in."
                .format(path))
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        currency = raw.get("currency", "USD")
        tiers = [Tier(currency=t.pop("currency", currency), **t)
                 for t in raw.pop("tiers", [])]
        raw["date"] = str(raw.get("date", ""))   # yaml turns dates into date objects
        known = set(cls.__dataclass_fields__)
        for key in [k for k in raw if k not in known]:
            raw.pop(key)                          # human reference notes
        for key in ("city", "region", "country", "kind", "summary", "phone",
                    "website"):
            if raw.get(key) is None:
                raw.pop(key, None)
        # organisers used to be a plain list of names; accept both shapes.
        raw["organisers"] = [o if isinstance(o, dict) else {"name": str(o)}
                             for o in (raw.get("organisers") or [])]
        event = cls(tiers=tiers, **raw)
        event.logo_dir = Path(path).parent / "logos"
        return event


# ---------------------------------------------------------------------------
# Voice: how the sender writes. Edit data/voice.yml.
# ---------------------------------------------------------------------------

TEAM_WORDS = ("team", "devrel", "community", "partnerships", "sponsorship",
              "marketing", "hello", "info", "events")


@dataclass
class Voice:
    sender_name: str = ""
    sender_role: str = ""
    club: str = ""
    greeting: str = "Hi {first},"
    signoff: str = ""
    max_words: int = 130
    tone_notes: list[str] = field(default_factory=list)
    phrases_i_use: list[str] = field(default_factory=list)
    phrases_i_avoid: list[str] = field(default_factory=list)
    sample_emails: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None, event: Event | None = None) -> "Voice":
        """voice.yml if present, else a plain voice built from the event."""
        path = Path(path or DATA / "voice.yml")
        raw: dict = {}
        if path.exists():
            import yaml  # noqa: PLC0415
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = set(cls.__dataclass_fields__)
        data = {k: v for k, v in raw.items() if k in known and v is not None}
        for key in ("tone_notes", "phrases_i_use", "phrases_i_avoid", "sample_emails"):
            if key in data and isinstance(data[key], str):
                data[key] = [data[key]]
            data[key] = [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]
        voice = cls(**data)
        if event is not None:
            voice.sender_name = voice.sender_name or event.sender_name
            voice.sender_role = voice.sender_role or event.sender_role
            voice.club = voice.club or event.club
        if not voice.signoff.strip():
            voice.signoff = "Thanks,\n{}\n{}, {}".format(
                voice.sender_name, voice.sender_role, voice.club).strip()
        voice.signoff = voice.signoff.strip()
        voice.max_words = int(voice.max_words or 130)
        return voice

    def greeting_for(self, contact_name: str = "", company: str = "") -> str:
        name = (contact_name or "").strip()
        low = name.lower()
        if not name:
            # Most role mailboxes -- sponsorships@, community@ -- come with no
            # person attached, and "Hi there," is what a mailshot opens with.
            # We always know the company even when we do not know a name, and
            # "DigitalOcean Team" is what a person would actually write.
            company = (company or "").strip()
            first = "{} Team".format(company) if company else "there"
        elif any(w in low for w in TEAM_WORDS) or "@" in name:
            if "team" in low:
                first = name
            elif (company or "").strip():
                first = "{} Team".format(company.strip())
            else:
                first = "team"
        else:
            first = name.split()[0]
        try:
            return self.greeting.format(first=first, team="team")
        except (KeyError, IndexError):
            return "Hi {},".format(first)

    def style_block(self) -> str:
        lines = ["Sender: {}, {}, {}".format(self.sender_name, self.sender_role,
                                             self.club),
                 "Greeting style: {}".format(self.greeting),
                 "Body length: under {} words".format(self.max_words)]
        if self.tone_notes:
            lines.append("How they write:")
            lines += ["- " + n for n in self.tone_notes]
        if self.phrases_i_use:
            lines.append("Phrases they actually use: " + "; ".join(
                '"{}"'.format(p) for p in self.phrases_i_use))
        if self.phrases_i_avoid:
            lines.append("Phrases they would never write: " + "; ".join(
                '"{}"'.format(p) for p in self.phrases_i_avoid))
        return "\n".join(lines)

    def examples_block(self) -> str:
        if not self.sample_emails:
            return "(no sample emails from the sender were provided)"
        out = ["Emails this person actually wrote (match this register):"]
        for i, sample in enumerate(self.sample_emails[:3], start=1):
            out.append("--- example {} ---\n{}".format(i, sample.strip()))
        return "\n".join(out)
