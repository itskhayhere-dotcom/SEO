"""Builds the self-hosted link graph: parse pages, store pages + links, snowball-crawl neighbours."""
from __future__ import annotations
import asyncio, re
from collections import Counter
from urllib.parse import urljoin, urldefrag, urlsplit
from bs4 import BeautifulSoup
from . import config, db, util
from .dedupe import normalize_url
from .crawler import Fetcher, Resp

SPAM_KW = re.compile(r"\b(casino|viagra|cialis|payday loan|porn|escort|betting|replica watches|buy followers|essay writing)\b", re.I)
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|rar|mp4|mp3|css|js|xml|json|woff2?|ttf|docx?|xlsx?|pptx?)(\?|$)", re.I)

def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")

def extract_links(soup: BeautifulSoup, base: str):
    """-> list of (absolute_url, anchor_text, nofollow)"""
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urldefrag(urljoin(base, href))[0]
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        anchor = a.get_text(" ", strip=True)
        if not anchor:
            img = a.find("img", alt=True)
            anchor = img["alt"].strip() if img else ""
        rel = " ".join(a.get("rel", [])).lower() if isinstance(a.get("rel"), list) else str(a.get("rel", "")).lower()
        out.append((url, anchor[:200], int(any(k in rel for k in ("nofollow", "ugc", "sponsored")))))
    return out

def store_page(resp: Resp, depth: int = 0, soup: BeautifulSoup | None = None):
    """Persist one fetched page and all its outbound links. Returns the soup for reuse."""
    if resp.status != 200 or not resp.text:
        return soup
    soup = soup or soup_of(resp.text)
    url, dom = resp.final_url, util.registered_domain(resp.final_url)
    links = extract_links(soup, url)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    ext = sum(1 for l, _, _ in links if util.registered_domain(l) != dom)
    title = (soup.title.string or "").strip()[:300] if soup.title and soup.title.string else ""
    ts = util.now()
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO domains(domain,tld,first_seen) VALUES (?,?,?)", (dom, dom.rsplit(".", 1)[-1], ts))
        c.execute("UPDATE domains SET last_crawled=? WHERE domain=?", (ts, dom))
        c.execute("""INSERT INTO pages(key,url,domain,status,fetched_at,lastmod,title,text_len,ext_out,kw_hits,depth)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(key) DO UPDATE SET url=excluded.url,status=excluded.status,fetched_at=excluded.fetched_at,
                       lastmod=excluded.lastmod,title=excluded.title,text_len=excluded.text_len,ext_out=excluded.ext_out,
                       kw_hits=excluded.kw_hits""",
                  (normalize_url(url), url, dom, 200, ts, _lastmod(resp), title, len(text), ext, len(SPAM_KW.findall(text)), depth))
        for dst, anchor, nofollow in links:
            ddom = util.registered_domain(dst)
            c.execute("INSERT OR IGNORE INTO domains(domain,tld,first_seen) VALUES (?,?,?)", (ddom, ddom.rsplit(".", 1)[-1], ts))
            c.execute("""INSERT INTO links(src_key,src_url,src_domain,dst_key,dst_url,dst_domain,anchor,nofollow,first_seen,last_seen)
                         VALUES (?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(src_key,dst_key) DO UPDATE SET last_seen=excluded.last_seen,anchor=excluded.anchor,nofollow=excluded.nofollow""",
                      (normalize_url(url), url, dom, normalize_url(dst), dst, ddom, anchor, nofollow, ts, ts))
    return soup

def _lastmod(resp: Resp):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(resp.headers["last-modified"]).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

async def crawl_domain(f: Fetcher, start: str, max_pages: int, delay: float | None = None, respect_robots: bool = True):
    """BFS over one registered domain. Returns number of pages stored."""
    dom = util.registered_domain(start)
    queue, seen, stored = [(start, 0)], {normalize_url(start)}, 0
    while queue and stored < max_pages:
        url, depth = queue.pop(0)
        r = await f.get(url, delay=delay, respect_robots=respect_robots)
        if r.status != 200 or "html" not in r.headers.get("content-type", "html"):
            continue
        soup = store_page(r, depth)
        stored += 1
        if depth >= 4:
            continue
        for l, _, _ in extract_links(soup, r.final_url):
            k = normalize_url(l)
            if util.registered_domain(l) == dom and k not in seen and not SKIP_EXT.search(l):
                seen.add(k); queue.append((l, depth + 1))
    return stored

async def snowball(f: Fetcher, start: str, own_pages: int | None = None, expand: int | None = None,
                   per_domain: int | None = None) -> dict:
    """Crawl the client's own site, then the most-linked external neighbours (shallow)."""
    own_pages = own_pages or config.GRAPH_OWN_PAGES
    expand = config.GRAPH_EXPAND_DOMAINS if expand is None else expand
    per_domain = per_domain or config.GRAPH_PAGES_PER_DOMAIN
    dom = util.registered_domain(start)
    n_own = await crawl_domain(f, start, own_pages, delay=config.OWN_SITE_DELAY)
    rows = db.q("SELECT dst_domain, COUNT(*) c FROM links WHERE src_domain=? AND dst_domain!=? GROUP BY 1 ORDER BY c DESC LIMIT ?",
                (dom, dom, expand))
    sem = asyncio.Semaphore(4)
    async def one(d):
        async with sem:
            return await crawl_domain(f, f"https://{d}/", per_domain)
    got = await asyncio.gather(*(one(r["dst_domain"]) for r in rows), return_exceptions=True)
    return {"own_pages": n_own, "neighbour_domains": len(rows), "neighbour_pages": sum(g for g in got if isinstance(g, int))}
