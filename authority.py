"""Self-built DA / PA / Spam Score.

DA  = damped PageRank over the *domain* graph (edge weight 1+ln(distinct linking pages), nofollow excluded),
      log-scaled to 0-100 relative to the strongest domain in the graph.
PA  = personalised PageRank over the *page* graph; teleport mass is proportional to the owning domain's PageRank,
      so pages on strong domains start stronger and internal links redistribute it. Same 0-100 log scaling.
Spam= weighted link-pattern heuristics, 0 (clean) - 100 (very spammy); see COMPONENTS. Each component that lacks
      data falls back to a neutral value and lowers the reported confidence.
"""
from __future__ import annotations
import json, math, re, socket
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from . import db, util
from .dedupe import normalize_anchor, normalize_url

def pagerank(out_edges: dict, teleport: dict | None = None, d: float = 0.85, tol: float = 1e-10, max_iter: int = 200) -> dict:
    nodes = set(out_edges)
    for v in out_edges.values():
        nodes.update(v)
    n = len(nodes)
    if not n:
        return {}
    tp = {k: 1.0 / n for k in nodes}
    if teleport:
        s = sum(teleport.get(k, 0.0) for k in nodes)
        if s > 0:
            tp = {k: teleport.get(k, 0.0) / s for k in nodes}
    out_w = {u: sum(t.values()) for u, t in out_edges.items()}
    pr = dict(tp)
    for _ in range(max_iter):
        dangling = sum(pr[u] for u in nodes if out_w.get(u, 0) <= 0)
        new = {k: (1 - d) * tp[k] + d * dangling * tp[k] for k in nodes}   # dangling mass follows the teleport vector
        for u, targets in out_edges.items():
            tot = out_w[u]
            if tot <= 0:
                continue
            share = d * pr[u] / tot
            for v, w in targets.items():
                new[v] += share * w
        err = sum(abs(new[k] - pr[k]) for k in nodes)
        pr = new
        if err < tol:
            break
    return pr

def scale_100(pr: dict) -> dict:
    """Mean-normalise then log-scale so the top node is 100. Scores are relative to *this* graph."""
    if not pr:
        return {}
    n = len(pr)
    v = {k: x * n for k, x in pr.items()}
    mx = max(v.values())
    den = math.log1p(mx) or 1.0
    return {k: round(100 * math.log1p(x) / den, 1) for k, x in v.items()}

# ------------------------------------------------------------------ spam heuristics
TLD_RISK = {**dict.fromkeys("tk ml ga cf gq top xyz click loan work win bid icu cyou buzz rest fit cam".split(), 0.9),
            **dict.fromkeys("info biz online site club shop live space website store".split(), 0.5),
            **dict.fromkeys("com org net edu gov ac in ae om co uk de io".split(), 0.1)}
MONEY = re.compile(r"\b(buy|cheap|best|price|discount|casino|loan|insurance|deal|coupon|order|online)\b")
NEUTRAL = {"velocity": 0.15, "anchor": 0.15, "neighbourhood": 0.30, "tld_host": 0.25, "content": 0.20}
WEIGHTS = {"velocity": 0.15, "anchor": 0.20, "neighbourhood": 0.25, "tld_host": 0.20, "content": 0.20}

def _clamp(v): return max(0.0, min(1.0, v))

def comp_velocity(times):
    """Share of inbound links dated inside one 7-day window (page Last-Modified, else first-seen)."""
    ts = sorted(times)
    if len(ts) < 5 or ts[-1] - ts[0] < timedelta(days=3):
        return None
    best = j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > timedelta(days=7):
            j += 1
        best = max(best, i - j + 1)
    return _clamp((best / len(ts) - 0.5) / 0.5)

def comp_anchor(anchors, brand):
    a = [normalize_anchor(x) for x in anchors if x and len(x) <= 80]
    a = [x for x in a if x and not x.startswith(("http", "www"))]
    if len(a) < 6:
        return None
    cnt = Counter(a); n = len(a)
    top_a, top_c = cnt.most_common(1)[0]
    conc = _clamp((top_c / n - 0.25) / 0.6) * (0.3 if brand and brand in top_a else 1.0)
    hhi = sum((c / n) ** 2 for c in cnt.values())
    money = sum(1 for x in a if MONEY.search(x)) / n
    return 0.5 * conc + 0.2 * _clamp((hhi - 0.15) / 0.6) + 0.3 * _clamp(money * 2)

def comp_neighbourhood(in_doms, out_doms, prior):
    parts = []
    if in_doms:
        avg = sum(prior.get(d, 35) for d in in_doms) / len(in_doms) / 100
        parts.append(_clamp((avg - 0.2) / 0.6))
    if out_doms:
        parts.append(sum(1 for d in out_doms if prior.get(d, 0) >= 60) / len(out_doms))
    return sum(parts) / len(parts) if parts else None

def comp_tld_host(tld, in_ips):
    tld_r = TLD_RISK.get(tld, 0.3)
    c24 = Counter(".".join(ip.split(".")[:3]) for ip in in_ips if ip and ip.count(".") == 3)
    if sum(c24.values()) >= 4:                       # PBN footprint: many referrers share one /24
        share = c24.most_common(1)[0][1] / sum(c24.values())
        return 0.6 * tld_r + 0.4 * _clamp((share - 0.3) / 0.7)
    return tld_r

def comp_content(p):
    if not p or not p["n"]:
        return None
    thin = _clamp((250 - (p["avg_text"] or 0)) / 250)
    ext = _clamp(((p["avg_ext"] or 0) - 15) / 45)
    kw = _clamp((p["kw"] or 0) / p["n"] * 3)
    return 0.35 * thin + 0.35 * ext + 0.30 * kw

def _resolve(domain):
    try:
        socket.setdefaulttimeout(4)
        return domain, socket.gethostbyname(domain)
    except Exception:
        return domain, None

def resolve_missing_ips(limit=300):
    rows = db.q("SELECT domain FROM domains WHERE ip IS NULL ORDER BY (last_crawled IS NULL), last_crawled DESC LIMIT ?", (limit,))
    if not rows:
        return
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(_resolve, [r["domain"] for r in rows]))
    with db.tx() as c:
        for d, ip in res:
            c.execute("UPDATE domains SET ip=? WHERE domain=?", (ip or "", d))

def compute_spam(domains, prior=None):
    """-> {domain: (score 0-100, detail dict)} using the stored link graph."""
    prior = prior or {}
    inb = defaultdict(list)
    for r in db.q("""SELECT l.dst_domain, l.src_domain, l.anchor, l.first_seen, p.lastmod
                     FROM links l LEFT JOIN pages p ON p.key=l.src_key WHERE l.src_domain!=l.dst_domain"""):
        inb[r["dst_domain"]].append(r)
    outb = defaultdict(set)
    for r in db.q("SELECT DISTINCT src_domain, dst_domain FROM links WHERE src_domain!=dst_domain"):
        outb[r["src_domain"]].add(r["dst_domain"])
    pstats = {r["domain"]: r for r in db.q("""SELECT domain, COUNT(*) n, AVG(text_len) avg_text, AVG(ext_out) avg_ext, SUM(kw_hits) kw
                                              FROM pages WHERE status=200 GROUP BY domain""")}
    info = {r["domain"]: r for r in db.q("SELECT domain, tld, ip FROM domains")}
    out = {}
    for d in domains:
        rows = inb.get(d, [])
        times = [t for t in (util.parse_ts(r["lastmod"] or r["first_seen"]) for r in rows) if t]
        label = d.split(".")[0]
        vals = {
            "velocity": comp_velocity(times),
            "anchor": comp_anchor([r["anchor"] for r in rows], label),
            "neighbourhood": comp_neighbourhood({r["src_domain"] for r in rows}, outb.get(d, set()), prior),
            "tld_host": comp_tld_host((info.get(d) or {"tld": d.rsplit(".", 1)[-1]})["tld"],
                                      [(info.get(r["src_domain"]) or {"ip": None})["ip"] for r in rows]),
            "content": comp_content(pstats.get(d)),
        }
        score = conf = 0.0
        comp = {}
        for k, w in WEIGHTS.items():
            v = vals[k]
            score += w * (NEUTRAL[k] if v is None else v)
            conf += w if v is not None else 0
            comp[k] = {"value": None if v is None else round(v, 3), "weight": w}
        out[d] = (round(100 * score, 1), {"components": comp, "confidence": round(conf, 2), "inbound_links": len(rows)})
    return out

# ------------------------------------------------------------------ traffic proxy
def traffic_proxy(da, sitemap_urls, fresh_ratio, referring_domains):
    size = min(1.0, math.log1p(sitemap_urls or 0) / math.log(5000))
    refs = min(1.0, math.log1p(referring_domains or 0) / math.log(50))
    return round(100 * (0.45 * size + 0.20 * (fresh_ratio or 0) + 0.15 * refs) + 0.20 * (da or 0), 1)

def rank_score(da, spam, traffic):
    return round(0.35 * (da or 0) + 0.20 * (traffic or 0) + 0.45 * (100 - (spam if spam is not None else 50)), 2)

# ------------------------------------------------------------------ full recompute
def recompute(tracked_domains=()) -> dict:
    """Recompute DA/PA/Spam for the whole graph, persist latest values, append history for tracked domains."""
    resolve_missing_ips()
    dom_edges = defaultdict(dict)
    for r in db.q("""SELECT src_domain, dst_domain, COUNT(DISTINCT src_key) c FROM links
                     WHERE nofollow=0 AND src_domain!=dst_domain GROUP BY 1,2"""):
        dom_edges[r["src_domain"]][r["dst_domain"]] = 1 + math.log(r["c"])
    all_domains = [r["domain"] for r in db.q("SELECT domain FROM domains")]
    for dname in all_domains:
        dom_edges.setdefault(dname, {})
    dpr = pagerank(dom_edges)
    da = scale_100(dpr)

    page_edges, node_dom = defaultdict(dict), {}
    for r in db.q("SELECT src_key, src_domain, dst_key, dst_domain FROM links WHERE nofollow=0 AND src_key!=dst_key"):
        page_edges[r["src_key"]][r["dst_key"]] = 1.0
        node_dom[r["src_key"]], node_dom[r["dst_key"]] = r["src_domain"], r["dst_domain"]
    per_dom = Counter(node_dom.values())
    tele = {k: dpr.get(dm, 0) / per_dom[dm] for k, dm in node_dom.items()}
    pa = scale_100(pagerank(page_edges, teleport=tele))

    prior = {d: s for d, s in ((r["domain"], r["spam"]) for r in db.q("SELECT domain, spam FROM domains")) if s is not None}
    s1 = compute_spam(all_domains, prior)
    s2 = compute_spam(all_domains, {d: v[0] for d, v in s1.items()})        # second pass uses pass-1 neighbour scores

    refs = {r["d"]: r["c"] for r in db.q("SELECT dst_domain d, COUNT(DISTINCT src_domain) c FROM links WHERE src_domain!=dst_domain GROUP BY 1")}
    ts, n = util.now(), len(dom_edges)
    with db.tx() as c:
        for d in all_domains:
            row = c.execute("SELECT sitemap_urls, fresh_ratio FROM domains WHERE domain=?", (d,)).fetchone()
            tr = traffic_proxy(da.get(d, 0), row["sitemap_urls"], row["fresh_ratio"], refs.get(d, 0))
            sp, det = s2[d]
            c.execute("UPDATE domains SET da=?, spam=?, traffic=?, scored_at=?, detail=? WHERE domain=?",
                      (da.get(d, 0), sp, tr, ts, json.dumps(det), d))
            if d in tracked_domains:
                c.execute("INSERT INTO domain_metrics(domain,da,spam,traffic,graph_nodes,computed_at,detail) VALUES (?,?,?,?,?,?,?)",
                          (d, da.get(d, 0), sp, tr, n, ts, json.dumps(det)))
        for t in c.execute("SELECT id, domain, norm_url FROM targets").fetchall():
            drow = c.execute("SELECT da, spam, traffic FROM domains WHERE domain=?", (t["domain"],)).fetchone()
            if not drow:
                continue
            p = pa.get(t["norm_url"], round(0.6 * (drow["da"] or 0), 1))
            c.execute("UPDATE targets SET da=?, pa=?, spam=?, traffic=?, rank_score=?, last_scored=? WHERE id=?",
                      (drow["da"], p, drow["spam"], drow["traffic"], rank_score(drow["da"], drow["spam"], drow["traffic"]), ts, t["id"]))
        for d in tracked_domains:
            for r in c.execute("SELECT key, url FROM pages WHERE domain=? AND status=200", (d,)).fetchall():
                if r["key"] in pa:
                    c.execute("INSERT INTO page_metrics(key,url,domain,pa,computed_at) VALUES (?,?,?,?,?)",
                              (r["key"], r["url"], d, pa[r["key"]], ts))
    return {"domains": n, "pages": len(node_dom), "tracked": list(tracked_domains)}
