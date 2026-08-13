"""Email extraction from the pages the enrich stage already fetches.

Google Maps never returns an email address, so it has to come from somewhere
else. For local businesses the highest-yield source by far is their own
contact page -- a funeral home or HVAC shop puts `info@` or the owner's
address right on the site. We are already downloading those pages for the
classifier, so this costs nothing extra.

Addresses are scored, not just collected: an address on the business's own
domain beats a free webmail one, a personal-looking local part beats a role
inbox, and `noreply@` is pushed to the bottom. `best_for` re-ranks with the
owner's name once that is known, so `margaret@riversidefh.com` wins over
`info@riversidefh.com` for a business owned by Margaret.
"""

from __future__ import annotations

import re

MAILTO_RE = re.compile(r"mailto:\s*([^\"'>\s?&]+)", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# "info [at] example [dot] com" and friends.
OBFUSCATED = [
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I), "."),
]

# Local parts that are never a person and rarely worth mailing.
JUNK_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "webmaster", "abuse", "privacy", "legal", "unsubscribe", "mailer-daemon",
    "bounce", "notifications", "wordpress", "sentry", "root",
}
# Hosts that appear in boilerplate, tracking pixels and CMS chrome.
JUNK_DOMAINS = {
    "example.com", "example.org", "domain.com", "yourdomain.com",
    "email.com", "sentry.io", "wixpress.com", "wix.com", "squarespace.com",
    "godaddy.com", "schema.org", "w3.org", "sentry-next.wixpress.com",
    "yoursite.com", "company.com", "test.com", "localhost",
    "mysite.com", "mywebsite.com", "emailprovider.tld",
}
# Web-design / marketing / CMS mailboxes that are not the business itself.
AGENCY_DOMAINS = {
    "wixpress.com", "wix.com", "squarespace.com", "weebly.com", "godaddy.com",
    "wordpress.com", "hubspot.com", "mailchimp.com", "constantcontact.com",
    "klaviyo.com", "shopify.com", "bigcommerce.com", "webflow.io",
    "bluehost.com", "siteground.com", "hostgator.com", "namecheap.com",
    "googlegroups.com", "googleusercontent.com",
}
PLACEHOLDER_LOCALS = {
    "example", "test", "testing", "sample", "demo", "yourname", "email",
    "username", "name", "user",
}
FILE_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".mp4", ".webp2x",
)
FREE_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "comcast.net", "sbcglobal.net", "att.net", "verizon.net",
    "msn.com", "live.com", "me.com", "mac.com", "protonmail.com",
}

# Role inboxes, best first. A real human beats all of these.
ROLE_SCORES = {
    "owner": 45, "president": 42, "founder": 42, "principal": 38,
    "gm": 34, "manager": 34, "director": 34,
    "info": 25, "contact": 25, "hello": 24, "office": 23, "admin": 22,
    "frontdesk": 20, "reception": 20, "sales": 20, "service": 16,
    "support": 14, "help": 12, "billing": 8, "accounting": 8,
    "careers": 2, "jobs": 2, "hr": 2, "media": 2, "press": 2,
}


def _clean(raw: str) -> str:
    e = raw.strip().strip(".,;:<>()[]\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    return e.split("?")[0]


def is_valid(email: str) -> bool:
    if email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or ".." in email or len(local) > 64:
        return False
    if local.startswith(".") or local.endswith(".") or domain.startswith("."):
        return False
    if "." not in domain or domain.endswith("."):
        return False
    if email.endswith(FILE_EXT):
        return False
    if domain in JUNK_DOMAINS or local in JUNK_LOCAL:
        return False
    # Cache-busted asset paths sometimes look like addresses.
    if re.fullmatch(r"[0-9a-f]{16,}", local):
        return False
    return True


def harvest(html: str) -> set[str]:
    """Every plausible address on a page: mailto links, text, obfuscated."""
    found: set[str] = set()
    for m in MAILTO_RE.findall(html or ""):
        e = _clean(m)
        if is_valid(e):
            found.add(e)

    text = html or ""
    for pattern, repl in OBFUSCATED:
        text = pattern.sub(repl, text)
    for m in EMAIL_RE.findall(text):
        e = _clean(m)
        if is_valid(e):
            found.add(e)
    return found


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) > 2}


# Host / TLD shapes that are almost always typos, never a real mailbox.
TYPO_TLDS = {".comp", ".con", ".cpm", ".comm", ".coom", ".netx", ".orgg"}
TYPO_HOSTS = {
    "yhaoo.com", "yahooo.com", "gamil.com", "gmial.com", "gmal.com",
    "hotmial.com", "outlok.com", "gmail.comp", "gmail.con",
}


def split_addresses(raw: str | None) -> list[str]:
    """Split comma/semicolon/whitespace-separated multi-value email cells."""
    if not raw:
        return []
    text = str(raw).replace(";", ",").replace("\n", ",")
    out: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        e = _clean(part)
        if not e or not is_valid(e) or e in seen:
            # Still try a regex find inside messy fragments.
            for m in EMAIL_RE.findall(part or ""):
                ee = _clean(m)
                if ee and is_valid(ee) and ee not in seen:
                    seen.add(ee)
                    out.append(ee)
            continue
        seen.add(e)
        out.append(e)
    return out


def score(email: str, site_domain: str = "", owner_name: str = "") -> float:
    """Higher is a better address to actually send to."""
    local, _, domain = email.partition("@")
    s = 0.0

    if site_domain and (domain == site_domain or domain.endswith("." + site_domain)):
        s += 120          # hard prefer the company's own domain
    elif domain in FREE_MAIL:
        s += 30           # plenty of local businesses really do use gmail
    else:
        s += 10

    if domain in TYPO_HOSTS or any(domain.endswith(t) for t in TYPO_TLDS):
        s -= 80           # jcbeach23@yhaoo.com / foo@gmail.comp lose hard

    base = re.sub(r"[^a-z]", "", local.lower())
    owner_tokens = _tokens(owner_name)
    if owner_tokens and any(t in base for t in owner_tokens):
        s += 60           # looks like the owner's personal address

    role = re.split(r"[^a-z]", local.lower())[0] if local else ""
    s += ROLE_SCORES.get(role, 0)
    if role not in ROLE_SCORES and not owner_tokens:
        # firstname / firstname.lastname -> probably a person
        if re.fullmatch(r"[a-z]{2,}(\.[a-z]{2,})?", local.lower()):
            s += 30
    return s


def rank(
    emails: list[str], site_domain: str = "", owner_name: str = ""
) -> list[str]:
    return sorted(
        set(emails),
        key=lambda e: (-score(e, site_domain, owner_name), e),
    )


def best_for(
    emails: list[str], site_domain: str = "", owner_name: str = ""
) -> str:
    ranked = rank(emails, site_domain, owner_name)
    return ranked[0] if ranked else ""


def is_placeholder_or_agency(
    email: str, business_domain: str = ""
) -> bool:
    """True when the address is boilerplate or an unrelated agency mailbox."""
    e = _clean(email or "")
    if not e or "@" not in e:
        return True
    local, _, domain = e.partition("@")
    biz = (business_domain or "").lower().strip()
    if domain in JUNK_DOMAINS or domain.endswith(".wixpress.com"):
        return True
    if local in PLACEHOLDER_LOCALS or local.startswith("example"):
        return True
    if e in {"example@mysite.com", "info@example.com", "email@domain.com"}:
        return True
    # Agency host that is not the business's own domain.
    if domain in AGENCY_DOMAINS and (not biz or domain != biz):
        return True
    return False


def is_clean_lead_email(email: str, business_domain: str = "") -> bool:
    e = _clean(email or "")
    if not e or not is_valid(e):
        return False
    return not is_placeholder_or_agency(e, business_domain)


def choose_primary(
    candidates: list[str],
    *,
    website: str = "",
    site_domain: str = "",
    owner_name: str = "",
    prefer: str = "",
) -> tuple[str, list[str]]:
    """Pick one primary address; return (primary, all_ranked).

    Prefers an address whose domain matches the website host. `prefer` (e.g.
    Shovels primary_email) is kept in the candidate set but does not win over
    a website-domain match or beat an obvious typo.
    """
    from .mapsdata import domain_of

    dom = (site_domain or domain_of(website) or "").lower()
    pool: list[str] = []
    seen: set[str] = set()
    for raw in list(candidates) + ([prefer] if prefer else []):
        for ee in split_addresses(raw):
            if ee not in seen:
                seen.add(ee)
                pool.append(ee)
    ranked = rank(pool, dom, owner_name)
    return (ranked[0] if ranked else ""), ranked
