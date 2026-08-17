"""Parsing for fields that hold several email addresses in one column.

A wholesale customer usually wants order paperwork going to more than one
mailbox — a buyer, an accounts inbox, a warehouse. Rather than a table per
address, the extra ones live in a single text column, and this turns that column
back into a clean list wherever mail is addressed.
"""
import re

# Imported customers with no real address on file get a synthetic one so the
# login row is valid. Mail must never be sent there.
PLACEHOLDER_DOMAIN = "@afblanks-noemail.invalid"

_SPLIT = re.compile(r"[,;\n\r]+")


def parse_email_list(raw: str | None) -> list[str]:
    """Split a stored multi-address field into unique, sendable addresses.

    Accepts commas, semicolons or newlines as separators, since the value may be
    typed by an admin as easily as posted by the registration form. Blanks,
    anything without an "@", and placeholder addresses are dropped; order is kept
    and duplicates removed case-insensitively.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in _SPLIT.split(raw):
        email = chunk.strip().strip("<>").strip()
        if not email or "@" not in email:
            continue
        if email.lower().endswith(PLACEHOLDER_DOMAIN):
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(email)
    return out


def join_email_list(emails: list[str] | None) -> str | None:
    """Render a list back into the stored form. None when nothing is left."""
    cleaned = parse_email_list("\n".join(emails or []))
    return "\n".join(cleaned) or None
