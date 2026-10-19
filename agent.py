"""Backlink agent: pick best unused targets for a client, reserve (dedupe) -> submit -> log -> verify later."""
from __future__ import annotations
import asyncio, json, random, time
from . import config, db, util, discovery
from .crawler import Fetcher
from .dedupe import reserve, normalize_url
from .submitter import BrowserSession

def anchors_for(client) -> list[str]:
    a = [x.strip() for x in json.loads(client["anchors"] or "[]") if x.strip()]
    return a or [client["name"], client["domain"], client["url"]]

def pick_anchor(client) -> str:
    used = {r["anchor"]: r["c"] for r in db.q("SELECT anchor, COUNT(*) c FROM backlinks WHERE client_id=? GROUP BY anchor", (client["id"],))}
    opts = anchors_for(client)
    low = min(used.get(a, 0) for a in opts)
    return random.choice([a for a in opts if used.get(a, 0) == low])

def eligible(client, limit: int):
    sql = """SELECT t.* FROM targets t WHERE t.status='ok' AND t.kind IN ('directory','guest_post') AND t.needs_account=0
             AND (t.country=? OR (t.is_global=1 AND ?)) AND t.spam<=? AND COALESCE(t.da,0)>=?
             AND NOT EXISTS (SELECT 1 FROM backlinks b WHERE b.client_id=? AND b.target_norm=t.norm_url)"""
    args = [client["country"], int(config.INCLUDE_GLOBAL), client["max_spam"], client["min_da"], client["id"]]
    if not config.ALLOW_SAME_DOMAIN:
        sql += """ AND t.domain NOT IN (SELECT t2.domain FROM backlinks b JOIN targets t2 ON t2.id=b.target_id WHERE b.client_id=?)"""
        args.append(client["id"])
    return db.q(sql + " ORDER BY t.rank_score DESC LIMIT ?", (*args, limit))

def _record(bid, res):
    if res.status == "submitted":
        db.x("""UPDATE backlinks SET status='submitted', submitted_at=?, listing_url=?, detail=?, retryable=0,
                verification='pending', next_verify_at=? WHERE id=?""",
             (util.now(), res.listing_url, res.detail, util.plus_hours(config.VERIFY_BACKOFF[0]), bid))
    elif res.status == "needs_manual":
        db.x("UPDATE backlinks SET status='needs_manual', detail=?, retryable=0 WHERE id=?", (res.detail, bid))
    else:
        b = db.one("SELECT attempts FROM backlinks WHERE id=?", (bid,))
        retry = res.retryable and b["attempts"] < config.MAX_SUBMIT_ATTEMPTS
        db.x("UPDATE backlinks SET status='failed', detail=?, retryable=?, next_retry_at=? WHERE id=?",
             (res.detail, int(retry), util.plus_hours(1 if b["attempts"] <= 1 else 24) if retry else None, bid))

async def run_backlinks(f: Fetcher, client_id: int) -> dict:
    c = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    if c["status"] != "active":
        return {"skipped": f"client is {c['status']}"}
    start, end = util.today_range_utc()
    done = db.one("SELECT COUNT(*) n FROM backlinks WHERE client_id=? AND submitted_at>=? AND submitted_at<? AND status IN ('submitted','verified')",
                  (client_id, start, end))["n"]
    need = c["daily_cap"] - done
    st = {"cap": c["daily_cap"], "already_today": done, "submitted": 0, "failed": 0, "needs_manual": 0, "discovered": 0, "pool": 0}
    if need <= 0:
        st["note"] = "daily cap already reached"; return st
    if len(eligible(c, need * 2)) < need * 1.5:                     # pool too thin -> discover + score more
        st["discovered"] = await discovery.top_up(f, c)
    deadline = time.monotonic() + config.RUN_MAX_MINUTES * 60
    retries = db.q("""SELECT * FROM backlinks WHERE client_id=? AND status='failed' AND retryable=1
                      AND target_id IS NOT NULL AND next_retry_at<=? LIMIT ?""", (client_id, util.now(), need))
    fresh = eligible(c, need * 4)
    st["pool"] = len(fresh)
    seen_domains: set[str] = set()
    async with BrowserSession() as sess:
        jobs = [("retry", r) for r in retries] + [("new", t) for t in fresh]
        for kind, t in jobs:
            if st["submitted"] >= need or time.monotonic() > deadline:
                break
            if kind == "retry":                                          # same backlink row is re-attempted: no new ledger entry
                bid, anchor, dest = t["id"], t["anchor"], t["dest_url"]
                tgt = db.one("SELECT * FROM targets WHERE id=?", (t["target_id"],))
            else:
                tgt = t
                if not config.ALLOW_SAME_DOMAIN and tgt["domain"] in seen_domains:
                    continue
                anchor, dest = pick_anchor(c), c["url"]
                bid = reserve(client_id, tgt["id"], tgt["url"], anchor, dest)
                if bid is None:
                    continue                                             # duplicate -> never submitted
            db.x("UPDATE backlinks SET attempts=attempts+1 WHERE id=?", (bid,))
            seen_domains.add(tgt["domain"])
            res = await sess.submit(f, tgt, c, anchor, dest)
            _record(bid, res)
            key = {"submitted": "submitted", "needs_manual": "needs_manual"}.get(res.status, "failed")
            st[key] += 1
            await asyncio.sleep(random.uniform(*config.SUBMIT_DELAY))
    if st["submitted"] < need:
        st["shortfall"] = need - st["submitted"]
        st["note"] = "target pool exhausted or run time limit hit; add seeds / SearXNG or relax max_spam / min_da"
    return st
