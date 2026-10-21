"""The HTML email: our fixed presentation, wrapped around the model's prose.

The split matters. Everything the *template* says -- the stats, what a sponsor
gets, the legacy line, who has backed us before -- comes from event.yml and is
true by construction, so it never needs grounding. Everything the *model*
writes is the personalisation: why this company, and which tier might suit
them. That part is short, it is the only part the verifier has to check, and
it is the only part that differs between one recipient and the next.

Written as inline-styled tables on purpose. Gmail, Outlook and every mobile
client strip <style> blocks and understand almost no modern CSS; a table with
inline styles is the one layout that survives all of them. No external images
either -- a logo hosted on a URL is blocked by default in most clients and
turns the email into a box of broken icons.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

# Pulled from the DotSlash 9.0 email so the palette stays the club's.
PURPLE = "#6c47c7"
TEAL = "#0a9396"
INK = "#333333"
MUTED = "#666666"
LINE = "#e6e6e6"
MAX_WIDTH = 640


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _paragraphs(prose: str) -> list[str]:
    """Blank-line separated blocks, which is how the model writes them."""
    return [p.strip() for p in re.split(r"\n\s*\n", prose or "") if p.strip()]


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

def _header(event) -> str:
    bits = ['<tr><td align="center" style="padding:28px 24px 8px 24px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:30px;'
            'letter-spacing:4px;font-weight:bold;color:{};">{}</div>'.format(
                PURPLE, esc(event.event_name).upper())]
    if event.theme:
        bits.append('<div style="font-family:Arial,Helvetica,sans-serif;'
                    'font-size:14px;letter-spacing:2px;color:{};padding-top:6px;">'
                    '{}</div>'.format(TEAL, esc(event.theme).upper()))
    if event.tagline:
        bits.append('<div style="font-family:Arial,Helvetica,sans-serif;'
                    'font-size:13px;color:{};padding-top:4px;">{}</div>'.format(
                        MUTED, esc(event.tagline)))
    line = "{} | 36-HOUR NATIONAL HACKATHON".format(esc(event.date).upper()) \
        if event.kind == "hackathon" else esc(event.date).upper()
    bits.append('<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
                'letter-spacing:1px;font-weight:bold;color:{};padding-top:10px;">'
                '{}</div>'.format(INK, line))
    bits.append('</td></tr><tr><td style="padding:14px 24px 0 24px;">'
                '<div style="border-top:3px solid {};"></div></td></tr>'.format(PURPLE))
    return "".join(bits)


def _prose(paragraphs: list[str]) -> str:
    if not paragraphs:
        return ""
    body = "".join(
        '<p style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.6;color:{};margin:0 0 14px 0;">{}</p>'.format(
            INK, esc(p).replace("\n", "<br>"))
        for p in paragraphs)
    return '<tr><td style="padding:22px 24px 4px 24px;">{}</td></tr>'.format(body)


def _stats(event) -> str:
    if not event.stats:
        return ""
    cells = []
    for i, stat in enumerate(event.stats):
        cells.append(
            '<td width="50%" style="padding:6px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid {};border-radius:6px;">'
            '<tr><td align="center" style="padding:14px 8px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:26px;'
            'font-weight:bold;color:{};">{}</div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            'color:{};padding-top:2px;">{}</div>'
            '</td></tr></table></td>'.format(
                PURPLE, TEAL, esc(stat.get("value")), MUTED, esc(stat.get("label"))))
    rows = ["<tr>{}</tr>".format("".join(cells[i:i + 2]))
            for i in range(0, len(cells), 2)]
    note = ""
    if event.stats_note:
        note = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
                'font-style:italic;color:{};text-align:center;padding-top:8px;">'
                '{}</div>'.format(MUTED, esc(event.stats_note)))
    return ('<tr><td style="padding:10px 18px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
            '{}</table>{}</td></tr>'.format("".join(rows), note))


def _section_title(text: str) -> str:
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:17px;'
            'font-weight:bold;color:{};border-left:4px solid {};padding-left:10px;'
            'margin:0 0 12px 0;">{}</div>'.format(TEAL, TEAL, esc(text)))


def _two_column_list(items: list[str]) -> str:
    cells = ['<td width="50%" valign="top" style="padding:3px 8px;'
             'font-family:Arial,Helvetica,sans-serif;font-size:14px;color:{};">'
             '&#8227; {}</td>'.format(MUTED, esc(item)) for item in items]
    if len(cells) % 2:
        cells.append('<td width="50%"></td>')
    rows = ["<tr>{}</tr>".format("".join(cells[i:i + 2]))
            for i in range(0, len(cells), 2)]
    return ('<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
            '{}</table>'.format("".join(rows)))


def _why_partner(event) -> str:
    if not event.why_partner:
        return ""
    return ('<tr><td style="padding:18px 24px 4px 24px;">{}{}</td></tr>'.format(
        _section_title("Why Partner With Us?"), _two_column_list(event.why_partner)))


def _offer(event, brochure: bool) -> str:
    if not event.what_we_offer:
        return ""
    cells = []
    for item in event.what_we_offer:
        cells.append(
            '<td width="50%" valign="top" style="padding:8px 10px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'font-weight:bold;color:{};">{}</div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'color:{};padding-top:2px;">{}</div></td>'.format(
                TEAL, esc(item.get("title")), MUTED, esc(item.get("detail"))))
    if len(cells) % 2:
        cells.append('<td width="50%"></td>')
    rows = ["<tr>{}</tr>".format("".join(cells[i:i + 2]))
            for i in range(0, len(cells), 2)]
    inner = ('<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
             '{}</table>'.format("".join(rows)))
    if brochure:
        inner += ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
                  'font-weight:bold;color:{};text-align:center;padding-top:12px;">'
                  'Please refer to the attached brochure for complete tier-wise '
                  'details.</div>'.format(TEAL))
    return ('<tr><td style="padding:18px 24px 4px 24px;">{}'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid {};border-radius:6px;">'
            '<tr><td style="padding:12px;">{}</td></tr></table></td></tr>'.format(
                _section_title("What We Offer Our Sponsors"), PURPLE, inner))


def _custom_packages(event) -> str:
    if not event.custom_packages:
        return ""
    return ('<tr><td align="center" style="padding:16px 24px 4px 24px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'font-weight:bold;color:{};">CUSTOM PACKAGES AVAILABLE</div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'color:{};padding-top:4px;">{}</div></td></tr>'.format(
                INK, MUTED, esc(event.custom_packages)))


def _legacy(event) -> str:
    if not event.legacy:
        return ""
    return ('<tr><td style="padding:18px 24px 4px 24px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid {};border-radius:6px;"><tr><td style="padding:14px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
            'font-weight:bold;color:{};padding-bottom:6px;">The {} Legacy</div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'line-height:1.6;color:{};">{}</div>'
            '</td></tr></table></td></tr>'.format(
                PURPLE, TEAL, esc(event.event_name.split()[0]), MUTED,
                esc(event.legacy)))


def _past_sponsors(event) -> str:
    if not event.past_sponsors:
        return ""
    strip = getattr(event, "past_sponsors_logo", "")
    cid = _cid_for(event, strip) if strip else ""
    if cid and cid in logo_files(event):
        # One image of the whole row, exactly as the 9.0 email had it. `alt`
        # carries the names so a client with images off still reads sensibly.
        names = ('<img src="cid:{}" alt="{}" width="560" style="display:block;'
                 'margin:0 auto;width:100%;max-width:560px;height:auto;'
                 'border:0;">'.format(cid, esc(", ".join(event.past_sponsors))))
    else:
        names = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
                 'color:{};line-height:2;text-align:center;">{}</div>'.format(
                     INK, esc("   ".join(event.past_sponsors))))
    return ('<tr><td style="padding:18px 24px 4px 24px;">{}'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid {};border-radius:6px;">'
            '<tr><td style="padding:14px;">{}'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            'color:{};text-align:center;padding-top:6px;">and many more</div>'
            '</td></tr></table></td></tr>'.format(
                _section_title("Trusted by Industry Leaders"), PURPLE, names, MUTED))


def logo_files(event, full: bool = True) -> dict:
    """{cid: Path} for every logo this email will reference.

    Returned so the caller can embed the files in the message itself. A logo
    fetched from a URL is blocked by default in Gmail and Outlook and renders
    as a broken box, which looks worse than no logo at all.
    """
    folder = getattr(event, "logo_dir", None)
    if folder is None:
        return {}
    folder = Path(folder)
    names: list = []
    if full:
        names += [o.get("logo") for o in (event.organisers or [])]
        names.append(getattr(event, "past_sponsors_logo", ""))
    names.append(getattr(event, "brand_logo", ""))
    out: dict = {}
    for name in names:
        if not name:
            continue
        path = folder / str(name)
        if path.is_file():
            out[_cid_for(event, name)] = path
    return out


def _cid_for(event, name: str) -> str:
    stem = Path(str(name)).stem
    return "logo-" + re.sub(r"[^A-Za-z0-9]", "-", stem).lower()


def partner_mailto(event, company: str = "") -> str:
    """The address the button opens, with the subject pre-filled so a reply
    lands somewhere recognisable rather than in a general inbox."""
    import urllib.parse  # noqa: PLC0415

    to = (event.reply_to or "").strip()
    if not to:
        return ""
    subject = "Sponsoring {}".format(event.event_name)
    if company:
        subject += " - {}".format(company)
    return "mailto:{}?{}".format(to, urllib.parse.urlencode(
        {"subject": subject}, quote_via=urllib.parse.quote))


def _cta(event, company: str = "") -> str:
    link = partner_mailto(event, company)
    label = "LET&#39;S PARTNER"
    if link:
        # The bulletproof-button shape: a table cell carrying the colour and
        # the radius, with an <a> padded inside it. Outlook ignores padding on
        # an <a>, and a <button> does not exist in email at all.
        button = ('<table cellpadding="0" cellspacing="0" role="presentation" '
                  'align="center" style="margin:10px auto 4px auto;">'
                  '<tr><td align="center" bgcolor="{purple}" '
                  'style="border-radius:6px;">'
                  '<a href="{href}" target="_blank" '
                  'style="display:inline-block;padding:13px 30px;'
                  'font-family:Arial,Helvetica,sans-serif;font-size:15px;'
                  'font-weight:bold;color:#ffffff;text-decoration:none;'
                  'border-radius:6px;">{label}</a>'
                  '</td></tr></table>'.format(purple=PURPLE, href=esc(link),
                                              label=label))
    else:
        button = ('<div style="font-family:Arial,Helvetica,sans-serif;'
                  'font-size:16px;font-weight:bold;color:{};padding:8px 0;">'
                  '{}</div>'.format(INK, label))
    return ('<tr><td style="padding:20px 24px 4px 24px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid {};border-radius:6px;">'
            '<tr><td align="center" style="padding:18px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:{};">Join us in backing the next group of student builders</div>'
            '{}'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'color:{};">or just reply to this email</div>'
            '</td></tr></table></td></tr>'.format(PURPLE, MUTED, button, MUTED))


def _organisers(event) -> str:
    if not event.organisers:
        return ""
    available = logo_files(event)
    cells = []
    for org in event.organisers:
        name = org.get("name", "")
        cid = _cid_for(event, org.get("logo", "")) if org.get("logo") else ""
        inner = ""
        if cid and cid in available:
            inner = ('<img src="cid:{}" alt="{}" height="56" '
                     'style="display:block;margin:0 auto 6px auto;height:56px;'
                     'width:auto;border:0;">'.format(cid, esc(name)))
        cells.append('<td align="center" valign="bottom" style="padding:6px 10px;">'
                     '{}<div style="font-family:Arial,Helvetica,sans-serif;'
                     'font-size:12px;color:{};">{}</div></td>'.format(
                         inner, TEAL, esc(name)))
    return ('<tr><td style="padding:18px 24px 4px 24px;">{}'
            '<table align="center" cellpadding="0" cellspacing="0" '
            'role="presentation"><tr>{}</tr></table></td></tr>'.format(
                _section_title("Organised By"), "".join(cells)))


def _signoff(event, voice, closing: str) -> str:
    bits = []
    brand = getattr(event, "brand_logo", "")
    if brand and _cid_for(event, brand) in logo_files(event, full=False):
        bits.append('<img src="cid:{}" alt="{}" height="44" '
                    'style="display:block;height:44px;width:auto;border:0;'
                    'margin:0 0 10px 0;">'.format(_cid_for(event, brand),
                                                  esc(event.event_name)))
    if closing:
        bits.append('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
                    'line-height:1.6;color:{};padding-bottom:12px;">{}</div>'.format(
                        INK, esc(closing)))
    signoff = (getattr(voice, "signoff", "") or "").strip()
    bits.append('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
                'line-height:1.6;color:{};">{}</div>'.format(
                    INK, esc(signoff).replace("\n", "<br>")))
    return '<tr><td style="padding:20px 24px 4px 24px;">{}</td></tr>'.format(
        "".join(bits))


def _opt_out(event, url: str = "") -> str:
    """The small print under the footer: the visible way out."""
    text = (getattr(event, "opt_out", "") or "").strip()
    if not text:
        return ""
    link = (' You can also <a href="{}" style="color:{};">use this link</a>.'
            .format(esc(url), MUTED) if url else "")
    return ('<tr><td style="padding:0 24px 22px 24px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
            'color:{};text-align:center;line-height:1.6;">{}{}</div>'
            '</td></tr>'.format(MUTED, esc(text), link))


def _footer(event) -> str:
    rows = []
    if event.reply_to:
        rows.append("Email: {}".format(esc(event.reply_to)))
    if event.website:
        rows.append("Website: {}".format(esc(event.website)))
    if event.instagram:
        rows.append("Instagram: {}".format(esc(event.instagram)))
    people = ["{} &#8211; {}".format(esc(c.get("name")), esc(c.get("phone")))
              for c in (event.team_contacts or []) if c.get("name")]
    if not people and event.phone:
        people = ["{} &#8211; {}".format(esc(event.sender_name), esc(event.phone))]
    block = "<br>".join(rows)
    if people:
        block += '<div style="padding-top:8px;">{}</div>'.format("<br>".join(people))
    if event.address:
        block += '<div style="padding-top:8px;">{}</div>'.format(esc(event.address))
    return ('<tr><td style="padding:18px 24px 26px 24px;">'
            '<div style="border-top:1px solid {};padding-top:14px;"></div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'font-weight:bold;color:{};text-align:center;padding-bottom:6px;">'
            'Contact Information</div>'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            'color:{};text-align:center;line-height:1.7;">{}</div>'
            '</td></tr>'.format(LINE, TEAL, MUTED, block))


# ---------------------------------------------------------------------------
# the whole email
# ---------------------------------------------------------------------------

def render_html(event, voice, body: str, closing: str = "",
                brochure: bool = False, full: bool = True,
                company: str = "", opt_out_url: str = "") -> str:
    """`body` is the model's personalised prose (greeting included).

    `full=False` renders only the prose and the sign-off, which is what a
    follow-up wants: nobody needs the stat boxes and the sponsor list three
    times in one thread.
    """
    rows = [_header(event), _prose(_paragraphs(body))]
    if full:
        rows += [_stats(event), _why_partner(event), _offer(event, brochure),
                 _custom_packages(event), _legacy(event), _past_sponsors(event),
                 _cta(event, company), _organisers(event)]
    rows += [_signoff(event, voice, closing), _footer(event),
             _opt_out(event, opt_out_url)]

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>{title}</title></head>'
        '<body style="margin:0;padding:0;background:#f4f4f6;">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        'style="background:#f4f4f6;"><tr><td align="center" style="padding:16px 8px;">'
        '<table width="{width}" cellpadding="0" cellspacing="0" role="presentation" '
        'style="max-width:{width}px;width:100%;background:#ffffff;'
        'border:1px solid {line};border-radius:8px;">{rows}</table>'
        '</td></tr></table></body></html>'
    ).format(title=esc(event.event_name), width=MAX_WIDTH, line=LINE,
             rows="".join(r for r in rows if r))


def render_text(event, voice, body: str, closing: str = "",
                brochure: bool = False, full: bool = True,
                opt_out_url: str = "") -> str:
    """The plain-text alternative. Every HTML email needs one: some clients
    show it, some filters score against it, and a text/plain-only message from
    a domain that usually sends multipart looks like a phish."""
    out = [body.strip()]
    if full:
        if event.stats:
            out.append("\n".join("{} {}".format(s.get("value"), s.get("label"))
                                 for s in event.stats)
                       + ("\n" + event.stats_note if event.stats_note else ""))
        if event.why_partner:
            out.append("WHY PARTNER WITH US\n" + "\n".join(
                "- " + w for w in event.why_partner))
        if event.what_we_offer:
            block = "WHAT WE OFFER OUR SPONSORS\n" + "\n".join(
                "- {}: {}".format(i.get("title"), i.get("detail"))
                for i in event.what_we_offer)
            if brochure:
                block += ("\nPlease refer to the attached brochure for complete "
                          "tier-wise details.")
            out.append(block)
        if event.custom_packages:
            out.append("CUSTOM PACKAGES AVAILABLE\n" + event.custom_packages)
        if event.legacy:
            out.append(event.legacy)
        if event.past_sponsors:
            out.append("Trusted by: " + ", ".join(event.past_sponsors)
                       + ", and many more")
        if event.organisers:
            out.append("Organised by " + ", ".join(
                o.get("name", "") for o in event.organisers))
        if event.reply_to:
            out.append("To partner with us, reply to this email or write to "
                       + event.reply_to)
    if closing:
        out.append(closing.strip())
    out.append((getattr(voice, "signoff", "") or "").strip())

    tail = []
    if event.reply_to:
        tail.append("Email: " + event.reply_to)
    if event.website:
        tail.append("Website: " + event.website)
    if event.instagram:
        tail.append("Instagram: " + event.instagram)
    for contact in event.team_contacts or []:
        if contact.get("name"):
            tail.append("{} - {}".format(contact["name"], contact.get("phone", "")))
    if not (event.team_contacts) and event.phone:
        tail.append("{} - {}".format(event.sender_name, event.phone))
    if event.address:
        tail.append(event.address)
    if tail:
        out.append("\n".join(tail))
    opt_out = (getattr(event, "opt_out", "") or "").strip()
    if opt_out:
        out.append(opt_out + (" You can also use this link: " + opt_out_url
                              if opt_out_url else ""))
    return "\n\n".join(part for part in out if part and part.strip())
