"""Read-only email access for Orion.

Multi-account IMAP client. Read-only by how we use it: we only ever issue
FETCH / SEARCH commands -- no STORE flags, no DELETE, no APPEND. Even if
Orion were compromised, the worst it could do is read mail.

Why IMAP + Gmail app password instead of the Gmail API
------------------------------------------------------
* Works for any provider, not just Gmail -- so a single code path covers
  the user's @gmail.com personal account and their Workspace-hosted
  @company address (which uses imap.gmail.com too) AND any future Outlook
  or Fastmail account.
* No Google Cloud Console clickthrough. Each account just needs an "app
  password" (Account -> Security -> 2-Step Verification -> App passwords).
* Standard-library only -- `imaplib`, `email`, `ssl`, all stdlib.

Setup
-----
In secrets_local.py:

    EMAIL_CREDENTIALS = {
        "personal": {
            "address":   "you@gmail.com",
            "password":  "abcd efgh ijkl mnop",     # 16-digit app password
            "imap_host": "imap.gmail.com",
        },
        "work": {
            "address":   "you@company.com",
            "password":  "...",
            "imap_host": "imap.gmail.com",          # Workspace = same host
        },
    }

In config.py, set EMAIL_ACCOUNTS to the keys you want active.
"""
import email
import email.header
import email.utils
import imaplib
import re
import ssl

import config


def _credentials():
    """Pull the email credentials dict out of secrets_local.py (if any)."""
    try:
        import secrets_local
        creds = getattr(secrets_local, "EMAIL_CREDENTIALS", None)
        return creds if isinstance(creds, dict) else {}
    except ImportError:
        return {}


def configured_accounts():
    """List of account NAMES (config keys) that have full credentials."""
    listed = getattr(config, "EMAIL_ACCOUNTS", None) or []
    creds = _credentials()
    out = []
    for name in listed:
        c = creds.get(name)
        if not isinstance(c, dict):
            continue
        if c.get("address") and c.get("password") and c.get("imap_host"):
            out.append(name)
    return out


def available():
    """True when at least one account is fully configured."""
    return bool(configured_accounts())


def _is_gmail(host):
    return "gmail.com" in (host or "").lower()


def _resolve(name):
    """Return (address, password, host, port) for a configured account."""
    c = _credentials().get(name) or {}
    return (c.get("address"), c.get("password"),
            c.get("imap_host"), int(c.get("imap_port", 993)))


# --- connection helper ---
class _Session:
    """Auto-closing IMAP session. Use as a context manager."""

    def __init__(self, name):
        self.name = name
        self.imap = None
        self.host = ""

    def __enter__(self):
        address, password, host, port = _resolve(self.name)
        if not all((address, password, host)):
            raise RuntimeError(f"account '{self.name}' missing credentials")
        self.host = host
        ctx = ssl.create_default_context()
        self.imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        self.imap.login(address, password)
        return self

    def __exit__(self, *_exc):
        try:
            if self.imap is not None:
                try:
                    self.imap.close()
                except Exception:
                    pass
                self.imap.logout()
        except Exception:
            pass


# --- header decoding ---
def _decode_header(raw):
    """Turn an RFC 2047 encoded header into plain text."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return " ".join(p.strip() for p in out if p).strip()


def _sender_display(from_header):
    """Extract a friendly name from a From header: 'Alex Smith <a@x.com>'
    -> 'Alex Smith' (or the address if no name was set)."""
    if not from_header:
        return ""
    name, addr = email.utils.parseaddr(from_header)
    name = _decode_header(name).strip()
    return name or addr or ""


# --- listing unread ---
def _unread_uids(session, exclude_promotions_social=True):
    """Return a list of UIDs of unread messages in INBOX. Gmail-aware:
    excludes the Promotions and Social categories when we're on Gmail."""
    session.imap.select("INBOX", readonly=True)
    if exclude_promotions_social and _is_gmail(session.host):
        # Gmail's IMAP supports X-GM-RAW with the same search operators as
        # the Gmail web UI. -category:promotions excludes that tab.
        query = ('X-GM-RAW', '"is:unread -category:promotions '
                              '-category:social"')
        typ, data = session.imap.uid("search", *query)
    else:
        typ, data = session.imap.uid("search", None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_envelopes(session, uids):
    """Fetch the headers of `uids` and return [{sender, subject, date, uid}]."""
    if not uids:
        return []
    set_str = b",".join(uids).decode()
    typ, data = session.imap.uid("fetch", set_str,
                                  "(BODY.PEEK[HEADER.FIELDS "
                                  "(FROM SUBJECT DATE)])")
    if typ != "OK":
        return []
    out = []
    # Pair each response item back to a UID from the original list. The
    # server response is interleaved tuples + ')' separators -- iterate and
    # only take the tuples.
    uid_iter = iter(uids)
    for item in data:
        if not isinstance(item, tuple):
            continue
        raw_headers = item[1]
        msg = email.message_from_bytes(raw_headers)
        out.append({
            "uid":     next(uid_iter, b"").decode(),
            "sender":  _sender_display(msg.get("From", "")),
            "subject": _decode_header(msg.get("Subject", "")) or "(no subject)",
            "date":    msg.get("Date", ""),
        })
    return out


def unread_summary():
    """Return {account_name: [envelope, ...]} for every configured account.

    Network calls run in parallel threads so two-account latency stays at
    about one round-trip, not two.
    """
    import threading
    results = {}
    errors = {}

    def _one(name):
        try:
            with _Session(name) as s:
                uids = _unread_uids(s)
                results[name] = _fetch_envelopes(s, uids)
        except Exception as e:
            errors[name] = str(e)
            results[name] = []

    threads = [threading.Thread(target=_one, args=(n,), daemon=True)
               for n in configured_accounts()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results, errors


def total_unread_count():
    """Quick total count across accounts (no envelopes). Used by the
    daily briefing offer."""
    res, _ = unread_summary()
    return sum(len(v) for v in res.values())


# --- body fetching ---
def _walk_text(msg):
    """Yield (mime_type, decoded_text) for every text part in `msg`."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    yield ct, payload.decode(charset, errors="replace")
                except LookupError:
                    yield ct, payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                yield msg.get_content_type(), \
                    payload.decode(charset, errors="replace")
            except LookupError:
                yield msg.get_content_type(), \
                    payload.decode("utf-8", errors="replace")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text):
    """Crude HTML -> text. Good enough for summarisation feed-in."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                  flags=re.S | re.I)
    text = _HTML_TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'"))
    return _WHITESPACE_RE.sub(" ", text).strip()


def fetch_body(account, uid, char_cap=None):
    """Return the plain-text body of one message, truncated to char_cap."""
    if char_cap is None:
        char_cap = getattr(config, "EMAIL_BODY_CAP", 4000)
    try:
        with _Session(account) as s:
            s.imap.select("INBOX", readonly=True)
            typ, data = s.imap.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                return ""
            raw = data[0][1]
    except Exception:
        return ""
    msg = email.message_from_bytes(raw)

    plain_chunks = []
    html_chunks = []
    for ct, text in _walk_text(msg):
        if ct == "text/plain":
            plain_chunks.append(text)
        elif ct == "text/html":
            html_chunks.append(text)
    body = " ".join(plain_chunks).strip()
    if not body and html_chunks:
        body = _strip_html(" ".join(html_chunks))
    body = _WHITESPACE_RE.sub(" ", body).strip()
    if len(body) > char_cap:
        body = body[:char_cap].rstrip() + " ..."
    return body


# --- search by sender (across all accounts) ---
def find_by_sender(query, limit=5):
    """Return [{account, uid, sender, subject, date}] of recent messages
    whose From matches the query string (case-insensitive, partial)."""
    query_low = (query or "").strip().lower()
    if not query_low:
        return []
    found = []
    for name in configured_accounts():
        try:
            with _Session(name) as s:
                s.imap.select("INBOX", readonly=True)
                # Search the whole INBOX, not just unread.
                typ, data = s.imap.uid("search", None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    continue
                uids = data[0].split()[-200:]  # last 200 only
                # Fetch envelopes for those, then filter locally.
                envs = _fetch_envelopes(s, uids)
                for env in envs:
                    if query_low in (env["sender"] or "").lower():
                        env["account"] = name
                        found.append(env)
        except Exception:
            continue
    # Newest-first by date. Fall back to insertion order if date is missing.
    def _key(e):
        try:
            return email.utils.parsedate_to_datetime(e.get("date") or "")
        except Exception:
            return None
    found.sort(key=lambda e: (_key(e) is None, _key(e)), reverse=True)
    return found[:limit]
