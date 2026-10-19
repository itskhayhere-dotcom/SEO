"""Target discovery + country filtering + scoring.

Sources (all self-hosted / free):  1) a self-hosted SearXNG instance (SEARXNG_URL),  2) seed "list" pages in
seeds/seeds.txt (their outbound links become candidates),  3) domains already in the link graph.
Comment-section targets are discovered and stored (kind='comment') but are MANUAL-ONLY: never auto-posted.
"""
from __future__ import annotations
import asyncio, math, re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import httpx
from . import config, db, util, linkgraph
from .crawler import Fetcher
from .dedupe import normalize_url
from . import authority

COUNTRIES = {
 "IN": dict(name="India", tld=(".in",), phone=("+91",), lang=("en-in", "hi"), geo=("india", "mumbai", "delhi", "bengaluru", "bangalore", "kochi", "kerala", "chennai", "hyderabad")),
 "OM": dict(name="Oman", tld=(".om",), phone=("+968",), lang=("en-om", "ar-om"), geo=("oman", "muscat", "salalah", "sohar")),
 "AE": dict(name="UAE", tld=(".ae",), phone=("+971",), lang=("en-ae", "ar-ae"), geo=("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah")),
 "SA": dict(name="Saudi Arabia", tld=(".sa",), phone=("+966",), lang=("en-sa", "ar-sa"), geo=("saudi", "riyadh", "jeddah")),
 "QA": dict(name="Qatar", tld=(".qa",), phone=("+974",), lang=("en-qa", "ar-qa"), geo=("qatar", "doha")),
 "KW": dict(name="Kuwait", tld=(".kw",), phone=("+965",), lang=("en-kw", "ar-kw"), geo=("kuwait",)),
 "BH": dict(name="Bahrain", tld=(".bh",), phone=("+973",), lang=("en-bh", "ar-bh"), geo=("bahrain", "manama")),
 "US": dict(name="United States", tld=(".us",), phone=("+1",), lang=("en-us",), geo=("usa", "united states", "new york", "california", "texas")),
 "GB": dict(name="United Kingdom", tld=(".uk",), phone=("+44",), lang=("en-gb",), geo=("united kingdom", "london", "england", "scotland")),
 "CA": dict(name="Canada", tld=(".ca",), phone=("+1",), lang=("en-ca", "fr-ca"), geo=("canada", "toronto", "vancouver")),
 "AU": dict(name="Australia", tld=(".au",), phone=("+61",), lang=("en-au",), geo=("australia", "sydney", "melbourne")),
 "SG": dict(name="Singapore", tld=(".sg",), phone=("+65",), lang=("en-sg",), geo=("singapore",)),
 "MY": dict(name="Malaysia", tld=(".my",), phone=("+60",), lang=("en-my", "ms"), geo=("malaysia", "kuala lumpur")),
 "DE": dict(name="Germany", tld=(".de",), phone=("+49",), lang=("de", "de-de"), geo=("germany", "deutschland", "berlin", "munich")),
}

def country_score(url: str, text: str, lang: str, code: str) -> float:
    c = COUNTRIES.get(code)
    if not c:
        return 0.0
    host, t, s = util.host_of(url), text.lower(), 0.0
    if any(host.endswith(x) for x in c["tld"]):
        s += 0.55
    if any((lang or "").lower().startswith(l) for l in c["lang"]) and "-" in (lang or ""):
        s += 0.25
    if any(p in text for p in c["phone"]):
        s += 0.2
    s += min(0.3, 0.1 * sum(1 for g in c["geo"] if g in t))
    return round(min(s, 1.0), 2)

def best_country(url, text, lang):
    sc = {k: country_score(url, text, lang, k) for k in COUNTRIES}
    k = max(sc, key=sc.get)
    return (k, sc[k]) if sc[k] > 0 else (None, 0.0)

_SUBMIT = re.compile(r"submit (your |a |an )?(site|website|business|listing|link|url)|add (your |a )?(business|listing|site|website|link|url)|free listing|list your (business|site|website)|register your (business|site)", re.I)
_GUEST = re.compile(r"write for us|guest post|submit (an? )?(article|guest)|contribute (an? )?article|become a contributor", re.I)
_FORUM = re.compile(r"phpbb|vbulletin|xenforo|discourse|invision (power|community)|mybb|simple machines", re.I)

def classify(soup, text: str, html: str) -> tuple[str | None, bool]:
    """-> (kind, needs_account)"""
    forms = soup.find_all("form")
    needs_acct = any(f.find("input", {"type": "password"}) for f in forms)
    if soup.find("textarea", {"name": "comment"}) or soup.find("form", id="commentform"):
        return "comment", needs_acct
    if _FORUM.search(html[:30000]):
        return "forum", needs_acct
    if _GUEST.search(text):
        return "guest_post", needs_acct
    if _SUBMIT.search(text) and forms:
        return "directory", needs_acct
    return None, needs_acct

def find_submit_link(soup, base):
    for a in soup.find_all("a", href=True):
        if _SUBMIT.search(a.get_text(" ", strip=True)) or _GUEST.search(a.get_text(" ", strip=True)):
            return urljoin(base, a["href"])
    return None

async def searx(f: Fetcher, query: str, code: str, pages: int = 2) -> list[str]:
    if not config.SEARXNG_URL:
        return []
    urls = []
    lang = (COUNTRIES.get(code, {}).get("lang") or ("en",))[0]
    async with httpx.AsyncClient(timeout=30) as c:
        for p in range(1, pages + 1):
            try:
                r = await c.get(config.SEARXNG_URL + "/search", params={"q": query, "format": "json", "language": lang, "pageno": p, "safesearch": 1})
                urls += [x["url"] for x in r.json().get("results", []) if x.get("url", "").startswith("http")]
            except Exception:
                break
            await asyncio.sleep(2)            # be gentle with the upstream engines SearXNG fans out to
    return urls

def queries_for(client) -> list[str]:
    cn = COUNTRIES.get(client["country"], {}).get("name", client["country"])
    n = client["niche"] or ""
    return [f"{n} business directory {cn}", f"submit your website {cn}", f"add your business listing {cn}",
            f"free business listing {cn}", f"{n} write for us {cn}", f"{n} guest post {cn}", f"{n} forum {cn}"]

def read_seeds() -> list[str]:
    p = Path(__file__).resolve().parent.parent / "seeds" / "seeds.txt"
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip().startswith("http")]

async def discover(f: Fetcher, client, max_new: int = 60) -> int:
    """Find new submission targets for the client's country. Returns number of targets stored."""
    cands: list[str] = []
    for qy in queries_for(client):
        cands += await searx(f, qy, client["country"])
    for seed in read_seeds():
        r = await f.get(seed)
        if r.status == 200:
            soup = linkgraph.soup_of(r.text)
            cands += [l for l, _, nf in linkgraph.extract_links(soup, r.final_url)
                      if util.registered_domain(l) != util.registered_domain(seed)][:150]
    seen, stored, tried = set(), 0, 0
    own = client["domain"]
    for url in cands:
        if stored >= max_new or tried >= max_new * 4:
            break
        n = normalize_url(url)
        dom = util.registered_domain(url)
        if n in seen or dom == own or db.one("SELECT 1 FROM targets WHERE norm_url=?", (n,)):
            continue
        seen.add(n); tried += 1
        r = await f.get(url)
        if r.status != 200:
            continue
        soup = linkgraph.soup_of(r.text)
        text = soup.get_text(" ", strip=True)
        kind, needs = classify(soup, text, r.text)
        if kind is None and urlsplit(url).path in ("", "/"):          # seed homepage: hop to its submit page
            sub = find_submit_link(soup, r.final_url)
            if sub and not db.one("SELECT 1 FROM targets WHERE norm_url=?", (normalize_url(sub),)):
                r2 = await f.get(sub)
                if r2.status == 200:
                    url, r, soup = sub, r2, linkgraph.soup_of(r2.text)
                    text = soup.get_text(" ", strip=True)
                    kind, needs = classify(soup, text, r2.text)
        if kind is None:
            continue
        lang = (soup.html.get("lang") if soup.html else "") or ""
        cc, sc = best_country(r.final_url, text, lang)
        is_global = int(cc is None and bool(re.search(r"worldwide|international|all countries|any country|globally", text, re.I)))
        if cc is None and not is_global:
            cc = None
        with db.tx() as c:
            c.execute("""INSERT OR IGNORE INTO targets(url,norm_url,domain,country,country_score,is_global,kind,source,needs_account,discovered_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (r.final_url, normalize_url(r.final_url), util.registered_domain(r.final_url), cc, sc, is_global,
                       kind, "searxng/seed", int(needs), util.now()))
        linkgraph.store_page(r, soup=None)
        stored += 1
    return stored

async def _sitemap_stats(f: Fetcher, domain: str):
    urls, fresh, total = 0, 0, 0
    r = await f.get(f"https://{domain}/sitemap.xml")
    if r.status != 200:
        return None, None
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    if "<sitemapindex" in r.text:
        for child in locs[:3]:
            rc = await f.get(child)
            if rc.status == 200:
                r_text = rc.text
                urls += len(re.findall(r"<loc>", r_text)); total += len(re.findall(r"<lastmod>", r_text))
                fresh += sum(1 for m in re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", r_text)
                             if (util.parse_ts(m + " 00:00:00") or util.parse_ts("2000-01-01 00:00:00")) > util.parse_ts(util.now()) - timedelta(days=90))
        urls = urls * max(1, len(locs) // 3) if len(locs) > 3 else urls
    else:
        urls, total = len(locs), len(re.findall(r"<lastmod>", r.text))
        fresh = sum(1 for m in re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", r.text)
                    if (util.parse_ts(m + " 00:00:00") or util.parse_ts("2000-01-01 00:00:00")) > util.parse_ts(util.now()) - timedelta(days=90))
    return urls, (fresh / total if total else 0.0)

async def score_new_targets(f: Fetcher, limit: int = 60) -> int:
    """Shallow-crawl each unscored target's domain, estimate size/freshness, then recompute the graph once."""
    rows = db.q("SELECT id, domain, url FROM targets WHERE status='new' ORDER BY discovered_at LIMIT ?", (limit,))
    doms = {r["domain"]: r["url"] for r in rows}
    sem = asyncio.Semaphore(4)
    async def one(d, u):
        async with sem:
            await linkgraph.crawl_domain(f, u, 5)
            s, fr = await _sitemap_stats(f, d)
            if s is not None:
                db.x("UPDATE domains SET sitemap_urls=?, fresh_ratio=? WHERE domain=?", (s, fr, d))
    await asyncio.gather(*(one(d, u) for d, u in doms.items()), return_exceptions=True)
    await asyncio.to_thread(authority.recompute, ())
    for r in rows:
        ok = db.one("SELECT spam FROM targets WHERE id=?", (r["id"],))
        db.x("UPDATE targets SET status=? WHERE id=?", ("ok" if ok and ok["spam"] is not None else "dead", r["id"]))
    return len(rows)

async def top_up(f: Fetcher, client, max_new: int = 60) -> int:
    n = await discover(f, client, max_new)
    await score_new_targets(f)
    return n
