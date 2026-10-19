"""Central configuration, read from environment / .env."""
from __future__ import annotations
import os, secrets
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def _s(k, d=""): return os.getenv(k, d).split("#")[0].strip()
def _i(k, d): return int(_s(k, str(d)))
def _f(k, d): return float(_s(k, str(d)))
def _b(k, d): return _s(k, str(d)).lower() in ("1", "true", "yes", "on")

DATA_DIR = Path(_s("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "seocrm.db"

HOST, PORT = _s("HOST", "127.0.0.1"), _i("PORT", 8000)
APP_PASSWORD_HASH = _s("APP_PASSWORD_HASH")
COOKIE_SECURE = _b("COOKIE_SECURE", False)
TIMEZONE = _s("TIMEZONE", "Asia/Kolkata")

def _session_secret() -> str:
    if _s("SESSION_SECRET"):
        return _s("SESSION_SECRET")
    p = DATA_DIR / ".session_secret"
    if not p.exists():
        p.write_text(secrets.token_hex(32)); p.chmod(0o600)
    return p.read_text().strip()
SESSION_SECRET = _session_secret()

USER_AGENT = _s("USER_AGENT", "SEOCRMBot/1.0 (polite crawler)")
AGENT_TOKEN = USER_AGENT.split("/")[0].strip() or "SEOCRMBot"
CRAWL_DELAY = _f("CRAWL_DELAY_SEC", 2.0)
OWN_SITE_DELAY = _f("OWN_SITE_DELAY_SEC", 0.5)
CONCURRENCY = _i("GLOBAL_CONCURRENCY", 8)
MAX_BYTES = _i("MAX_RESPONSE_BYTES", 2_000_000)
ALLOW_PRIVATE = _b("ALLOW_PRIVATE_HOSTS", False)

GRAPH_OWN_PAGES = _i("GRAPH_OWN_PAGES", 60)
GRAPH_EXPAND_DOMAINS = _i("GRAPH_EXPAND_DOMAINS", 30)
GRAPH_PAGES_PER_DOMAIN = _i("GRAPH_PAGES_PER_DOMAIN", 6)
SEARXNG_URL = _s("SEARXNG_URL").rstrip("/")

SUBMIT_DELAY = (_f("SUBMIT_DELAY_MIN_SEC", 20), _f("SUBMIT_DELAY_MAX_SEC", 60))
RUN_MAX_MINUTES = _i("RUN_MAX_MINUTES", 90)
STRICT_TARGET_DEDUPE = _b("STRICT_TARGET_DEDUPE", True)
ALLOW_SAME_DOMAIN = _b("ALLOW_SAME_DOMAIN", False)
INCLUDE_GLOBAL = _b("INCLUDE_GLOBAL_TARGETS", True)
MAX_SUBMIT_ATTEMPTS = _i("MAX_SUBMIT_ATTEMPTS", 3)
VERIFY_BACKOFF = [int(x) for x in _s("VERIFY_BACKOFF_HOURS", "2,24,72,168,336").split(",") if x]
HEADLESS = _b("PLAYWRIGHT_HEADLESS", True)

AUDIT_MAX_PAGES = _i("AUDIT_MAX_PAGES", 200)
AUDIT_LINK_CHECK_MAX = _i("AUDIT_LINK_CHECK_MAX", 300)
AUDIT_BROWSER_SAMPLES = _i("AUDIT_BROWSER_SAMPLES", 3)

SHEETS_BACKEND = _s("SHEETS_BACKEND", "none").lower()
SHEET_NAME = _s("SHEET_NAME", "SEO")
SHEET_ID = _s("SHEET_ID")
SA_FILE = _s("GOOGLE_SERVICE_ACCOUNT_FILE", "./secrets/service-account.json")
OAUTH_TOKEN_FILE = _s("GOOGLE_OAUTH_TOKEN_FILE", "./secrets/oauth-token.json")
APPS_SCRIPT_URL = _s("APPS_SCRIPT_URL")
APPS_SCRIPT_SECRET = _s("APPS_SCRIPT_SECRET")

CATCHUP = _b("CATCHUP_ON_START", True)
