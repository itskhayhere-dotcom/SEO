"""Backlink de-duplication ledger.

A backlink's identity for one client is sha256(normalized target URL | normalized anchor | normalized destination).
Uniqueness is enforced twice: (1) a pre-check inside a BEGIN IMMEDIATE transaction, and (2) the
UNIQUE(client_id, norm_hash) constraint in SQLite, so even a bug or a race cannot create a duplicate.
With STRICT_TARGET_DEDUPE (default) a target URL is also used only once per client regardless of anchor.
Rows are *reserved before* submission, so a crash mid-submit can never lead to a second attempt creating a twin.
"""
from __future__ import annotations
import hashlib, re, sqlite3, unicodedata
from urllib.parse import urlsplit, parse_qsl, urlencode, unquote
from . import config, db

_TRACK = re.compile(r"^(utm_.*|fbclid|gclid|msclkid|mc_eid|ref|ref_src|igshid|yclid|_ga|source)$", re.I)
_INDEX = re.compile(r"/(index|default|home)\.(html?|php|aspx?)$", re.I)

def normalize_url(u: str) -> str:
    """Canonical key: scheme-less, lowercase host without www/port-80/443, no fragment/tracking params,
    sorted query, collapsed slashes, no trailing slash or index file."""
    u = (u or "").strip()
    if "://" not in u:
        u = "http://" + u
    p = urlsplit(u)
    host = (p.hostname or "").lower().rstrip(".")
    try:
        host = host.encode("idna").decode()
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    try:
        port = p.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        host += f":{port}"
    path = re.sub(r"/{2,}", "/", unquote(p.path) or "/")
    path = _INDEX.sub("/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACK.match(k))
    return host + (path or "/") + ("?" + urlencode(query) if query else "")

def normalize_anchor(a: str) -> str:
    a = unicodedata.normalize("NFKC", a or "").casefold()
    a = re.sub(r"\s+", " ", a).strip()
    return a.strip(" .,;:!?\"'()[]{}|-\u2013\u2014/")

def link_hash(target_url: str, anchor: str, dest_url: str) -> str:
    raw = "|".join((normalize_url(target_url), normalize_anchor(anchor), normalize_url(dest_url)))
    return hashlib.sha256(raw.encode()).hexdigest()

def is_duplicate(client_id: int, target_url: str, anchor: str, dest_url: str) -> bool:
    h = link_hash(target_url, anchor, dest_url)
    if db.one("SELECT 1 FROM backlinks WHERE client_id=? AND norm_hash=?", (client_id, h)):
        return True
    return bool(config.STRICT_TARGET_DEDUPE and db.one(
        "SELECT 1 FROM backlinks WHERE client_id=? AND target_norm=?", (client_id, normalize_url(target_url))))

def reserve(client_id: int, target_id, target_url: str, anchor: str, dest_url: str, strict: bool | None = None):
    """Atomically claim (client, target, anchor, dest). Returns backlink id, or None if it is a duplicate."""
    strict = config.STRICT_TARGET_DEDUPE if strict is None else strict
    h, tn = link_hash(target_url, anchor, dest_url), normalize_url(target_url)
    try:
        with db.tx() as c:
            if c.execute("SELECT 1 FROM backlinks WHERE client_id=? AND norm_hash=?", (client_id, h)).fetchone():
                return None
            if strict and c.execute("SELECT 1 FROM backlinks WHERE client_id=? AND target_norm=?", (client_id, tn)).fetchone():
                return None
            cur = c.execute(
                "INSERT INTO backlinks(client_id,target_id,target_url,target_norm,anchor,dest_url,norm_hash) VALUES (?,?,?,?,?,?,?)",
                (client_id, target_id, target_url, tn, anchor, dest_url, h))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
