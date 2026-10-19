"""Website audit: crawl the client's own site and produce a prioritised, severity-tagged issue list."""
from __future__ import annotations
import asyncio, json, re
from collections import defaultdict
from urllib.parse import urljoin, urlsplit
from . import config, db, util, linkgraph
from .crawler import Fetcher
from .dedupe import normalize_url

SEV_W = {"critical": 10, "high": 5, "medium": 2, "low": 0.5, "info": 0}
ORDER = ["critical", "high", "medium", "low", "info"]

async def _browser_metrics(urls):
    """Local lab metrics (load, DOMContentLoaded, LCP) for a few pages via headless Chromium."""
    out = {}
    if not urls:
        return out
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            for u in urls:
                ctx = await b.new_context(user_agent=config.USER_AGENT)
                p = await ctx.new_page()
                await p.add_init_script("window.__lcp=0;new PerformanceObserver(l=>{for(const e of l.getEntries())window.__lcp=e.startTime}).observe({type:'largest-contentful-paint',buffered:true})")
                try:
                    await p.goto(u, timeout=30000, wait_until="load")
                    await p.wait_for_timeout(1000)
                    out[u] = await p.evaluate("""()=>{const n=performance.getEntriesByType('navigation')[0];
                        return {dcl:Math.round(n.domContentLoadedEventEnd),load:Math.round(n.loadEventEnd),lcp:Math.round(window.__lcp),
                                requests:performance.getEntriesByType('resource').length,
                                kb:Math.round(performance.getEntriesByType('resource').reduce((s,r)=>s+(r.transferSize||0),0)/1024)}}""")
                except Exception as e:
                    out[u] = {"error": type(e).__name__}
                await ctx.close()
            await b.close()
    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {e}"
    return out

async def run_audit(f: Fetcher, client) -> int:
    start = client["url"]
    host_dom = util.registered_domain(start)
    audit_id = db.x("INSERT INTO audits(client_id, started_at) VALUES (?,?)", (client["id"], util.now()))
    issues: list[tuple] = []
    add = lambda sev, code, url, detail="": issues.append((sev, code, url, detail))

    # robots.txt / sitemap
    origin = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"
    rob = await f.get(origin + "/robots.txt", respect_robots=False, delay=config.OWN_SITE_DELAY)
    rp, _ = await f.robots_for(start)
    if rob.status != 200:
        add("low", "robots_txt_missing", origin + "/robots.txt", f"HTTP {rob.status or rob.error}")
    elif re.search(r"(?im)^user-agent:\s*\*\s*\n(?:\s*#.*\n|\s*(?:allow|crawl-delay):.*\n)*\s*disallow:\s*/\s*$", rob.text):
        add("critical", "robots_blocks_all", origin + "/robots.txt", "Disallow: / for all user agents")
    sm_urls = set()
    sm = await f.get(origin + "/sitemap.xml", respect_robots=False, delay=config.OWN_SITE_DELAY)
    if sm.status == 200:
        sm_urls = {normalize_url(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text) if not u.endswith(".xml")}
    else:
        add("low", "sitemap_missing", origin + "/sitemap.xml", f"HTTP {sm.status or sm.error}")

    # crawl (BFS with depth); robots is not obeyed on the owner's own site, but disallowed URLs are reported
    pages, queue, seen = {}, [(start, 0)], {normalize_url(start)}
    link_targets = defaultdict(set)                    # url -> pages linking to it
    titles, descs = defaultdict(list), defaultdict(list)
    while queue and len(pages) < config.AUDIT_MAX_PAGES:
        url, depth = queue.pop(0)
        r = await f.get(url, respect_robots=False, delay=config.OWN_SITE_DELAY)
        pg = {"url": url, "depth": depth, "status": r.status, "ttfb": r.elapsed_ms, "bytes": len(r.text.encode()),
              "chain": r.chain, "err": r.error}
        pages[url] = pg
        if len(r.chain) > 2:
            add("medium" if len(r.chain) < 4 else "high", "redirect_chain", url, " -> ".join(f"{u} [{s}]" for u, s in r.chain))
        if r.error == "redirect_loop":
            add("critical", "redirect_loop", url, "redirect loop")
        if r.status >= 500:
            add("critical", "page_5xx", url, f"HTTP {r.status}")
        elif r.status >= 400:
            add("high", "page_4xx", url, f"HTTP {r.status}")
        if r.status != 200 or "html" not in r.headers.get("content-type", "html"):
            continue
        soup = linkgraph.soup_of(r.text)
        linkgraph.store_page(r, depth)                    # feeds the DA/PA graph too
        fu = r.final_url
        if rp is not None and not rp.can_fetch(config.AGENT_TOKEN, fu):
            add("medium", "blocked_by_robots", fu, "URL is crawlable by you but disallowed in robots.txt")
        if fu.startswith("http://"):
            add("high", "not_https", fu)
        # head signals
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        desc = (md.get("content") or "").strip() if md else ""
        titles[title].append(fu); descs[desc].append(fu)
        if not title: add("high", "missing_title", fu)
        elif len(title) > 60: add("low", "title_too_long", fu, f"{len(title)} chars")
        elif len(title) < 15: add("low", "title_too_short", fu, f"{len(title)} chars")
        if not desc: add("medium", "missing_meta_description", fu)
        elif len(desc) > 160: add("low", "meta_description_too_long", fu, f"{len(desc)} chars")
        # headings
        hs = [int(h.name[1]) for h in soup.find_all(re.compile(r"^h[1-6]$"))]
        if hs.count(1) == 0: add("medium", "missing_h1", fu)
        elif hs.count(1) > 1: add("low", "multiple_h1", fu, f"{hs.count(1)} h1 tags")
        if any(b - a > 1 for a, b in zip(hs, hs[1:])): add("low", "heading_level_skipped", fu, "e.g. h1 -> h3")
        # images
        imgs = soup.find_all("img")
        noalt = [i for i in imgs if i.get("alt") is None or not i.get("alt").strip()]
        if noalt: add("medium", "img_missing_alt", fu, f"{len(noalt)} of {len(imgs)} images")
        if any(i.get("src", "").startswith("http://") for i in imgs) and fu.startswith("https://"):
            add("medium", "mixed_content", fu, "http:// images on https page")
        # canonical / robots meta / viewport / lang
        can = soup.find("link", rel="canonical")
        if not can or not can.get("href"): add("low", "canonical_missing", fu)
        else:
            cu = urljoin(fu, can["href"])
            if normalize_url(cu) != normalize_url(fu):
                add("medium", "canonical_points_elsewhere", fu, cu); link_targets[cu].add(fu)
        rm = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
        if "noindex" in ((rm.get("content") or "").lower() if rm else "") or "noindex" in r.headers.get("x-robots-tag", "").lower():
            add("critical" if depth == 0 else "info", "noindex", fu, "page excluded from search")
        if not soup.find("meta", attrs={"name": "viewport"}): add("medium", "missing_viewport", fu)
        if not (soup.html and soup.html.get("lang")): add("low", "missing_lang", fu)
        # local speed indicators
        if pg["ttfb"] > 1800: add("high", "slow_ttfb", fu, f"{pg['ttfb']} ms")
        elif pg["ttfb"] > 800: add("medium", "slow_ttfb", fu, f"{pg['ttfb']} ms")
        if pg["bytes"] > 500_000: add("medium", "large_html", fu, f"{pg['bytes']//1024} KB")
        if "content-encoding" not in {k.lower() for k in r.headers}: add("low", "no_compression", fu)
        blocking = [s for s in soup.find_all("script", src=True) if not (s.get("async") or s.get("defer"))]
        if len(blocking) > 5: add("low", "render_blocking_scripts", fu, f"{len(blocking)} sync scripts")
        words = len(re.findall(r"\w+", soup.get_text(" ")))
        if words < 200: add("low", "thin_content", fu, f"{words} words")
        pg["title"] = title
        # links
        for l, _, _ in linkgraph.extract_links(soup, fu):
            link_targets[l].add(fu)
            k = normalize_url(l)
            if util.registered_domain(l) == host_dom and k not in seen and not linkgraph.SKIP_EXT.search(l):
                seen.add(k); queue.append((l, depth + 1))
        if depth > 5: add("medium", "deep_page", fu, f"click depth {depth}")
        elif depth > 3: add("low", "deep_page", fu, f"click depth {depth}")

    for t, urls in titles.items():
        if t and len(urls) > 1: add("medium", "duplicate_title", urls[0], f"'{t[:70]}' on {len(urls)} pages")
    for d, urls in descs.items():
        if d and len(urls) > 1: add("medium", "duplicate_meta_description", urls[0], f"on {len(urls)} pages")
    crawled = {normalize_url(u) for u in pages}
    for u in sm_urls - crawled:
        if len(pages) < config.AUDIT_MAX_PAGES: add("medium", "sitemap_url_not_reached", u, "listed in sitemap but not reachable via internal links (orphan?)")

    # broken links: HEAD (GET fallback), polite per-host pacing, capped
    sem, checked = asyncio.Semaphore(8), {}
    todo = [u for u in link_targets if normalize_url(u) not in crawled][:config.AUDIT_LINK_CHECK_MAX]
    async def chk(u):
        async with sem:
            r = await f.get(u, method="HEAD", respect_robots=False, max_hops=5)
            if r.status in (405, 403, 501) or (r.error and r.status == 0 and r.error != "blocked_or_unresolvable_host"):
                r = await f.get(u, respect_robots=False, max_hops=5)
            checked[u] = r
    await asyncio.gather(*(chk(u) for u in todo))
    for u, r in checked.items():
        bad = r.error in ("ConnectError", "ConnectTimeout", "blocked_or_unresolvable_host") or r.status in (404, 410) or r.status >= 500
        if not bad: continue
        internal = util.registered_domain(u) == host_dom
        for src in list(link_targets[u])[:5]:
            add("high" if internal else "medium", "broken_internal_link" if internal else "broken_external_link", src,
                f"{u} -> {r.status or r.error}")
    for p in pages.values():                                     # internal links to crawled pages that returned errors
        if p["status"] >= 400:
            for src in list(link_targets.get(p["url"], []))[:5]:
                add("high", "broken_internal_link", src, f"{p['url']} -> {p['status']}")

    samples = [u for u, p in pages.items() if p["status"] == 200][:config.AUDIT_BROWSER_SAMPLES]
    bm = await _browser_metrics(samples) if config.AUDIT_BROWSER_SAMPLES else {}
    for u, m in bm.items():
        if u.startswith("_") or "error" in m: continue
        if m["lcp"] > 4000: add("high", "slow_lcp", u, f"LCP {m['lcp']} ms (local lab)")
        elif m["lcp"] > 2500: add("medium", "slow_lcp", u, f"LCP {m['lcp']} ms (local lab)")

    # persist
    n = max(1, len(pages))
    penalty = sum(SEV_W[s] for s, *_ in issues) / n * 5
    score = max(0, round(100 - min(100, penalty)))
    counts = {s: sum(1 for i in issues if i[0] == s) for s in ORDER}
    with db.tx() as c:
        c.executemany("INSERT INTO audit_issues(audit_id,severity,code,url,detail) VALUES (?,?,?,?,?)", [(audit_id, *i) for i in issues])
        c.executemany("INSERT INTO audit_pages(audit_id,url,status,depth,ttfb_ms,bytes,title,metrics) VALUES (?,?,?,?,?,?,?,?)",
                      [(audit_id, p["url"], p["status"], p["depth"], p["ttfb"], p["bytes"], p.get("title"), json.dumps(bm.get(p["url"], {}))) for p in pages.values()])
        c.execute("UPDATE audits SET finished_at=?, pages=?, score=?, summary=? WHERE id=?",
                  (util.now(), len(pages), score, json.dumps(counts), audit_id))
    return audit_id
