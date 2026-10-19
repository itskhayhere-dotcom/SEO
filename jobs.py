"""Job runner: run tracking, per-(client,job) mutual exclusion, startup recovery, nightly DB backup."""
from __future__ import annotations
import asyncio, json, logging, shutil, sqlite3
from datetime import datetime
from . import config, db, util, linkgraph, authority, audit, agent, verify, sheets, discovery
from .crawler import shared

log = logging.getLogger("seocrm.jobs")
_running: set[tuple] = set()
_tasks: set[asyncio.Task] = set()

def is_running(client_id, job) -> bool:
    return (client_id, job) in _running

async def tracked(client_id, job: str, trigger: str, fn):
    key = (client_id, job)
    if key in _running:
        log.info("skip %s: already running", key); return None
    _running.add(key)
    rid = db.x("INSERT INTO runs(client_id,job,trigger,status,started_at) VALUES (?,?,?,'running',?)", (client_id, job, trigger, util.now()))
    try:
        stats = await fn()
        db.x("UPDATE runs SET status='ok', finished_at=?, stats=? WHERE id=?", (util.now(), json.dumps(stats, default=str), rid))
        return stats
    except Exception as e:
        log.exception("job %s failed", key)
        db.x("UPDATE runs SET status='error', finished_at=?, error=? WHERE id=?", (util.now(), f"{type(e).__name__}: {e}"[:500], rid))
    finally:
        _running.discard(key)

# ---- job bodies
async def _metrics(cid):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    f = shared()
    st = await linkgraph.snowball(f, c["url"])
    if config.SEARXNG_URL:                                                # inbound discovery via self-hosted search
        urls = await discovery.searx(f, f'"{c["domain"]}" -site:{c["domain"]}', c["country"], pages=2)
        for u in urls[:40]:
            r = await f.get(u)
            if r.status == 200: linkgraph.store_page(r)
        st["inbound_candidates"] = len(urls)
    sm = await discovery._sitemap_stats(f, c["domain"])
    if sm[0] is not None:
        db.x("UPDATE domains SET sitemap_urls=?, fresh_ratio=? WHERE domain=?", (sm[0], sm[1], c["domain"]))
    st.update(await asyncio.to_thread(authority.recompute, (c["domain"],)))
    return st

async def _backlinks(cid): return await agent.run_backlinks(shared(), cid)
async def _audit(cid):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    aid = await audit.run_audit(shared(), c)
    return {"audit_id": aid}
async def _discover(cid):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    return {"discovered": await discovery.top_up(shared(), c)}
async def _verify(cid): return await verify.sweep(shared(), force_client=cid)

BODIES = {"backlinks": _backlinks, "audit": _audit, "metrics": _metrics, "discover": _discover, "verify": _verify}

def launch(client_id: int, job: str, trigger: str = "manual") -> bool:
    if is_running(client_id, job):
        return False
    t = asyncio.create_task(tracked(client_id, job, trigger, lambda: BODIES[job](client_id)))
    _tasks.add(t); t.add_done_callback(_tasks.discard)
    return True

async def run_scheduled(client_id: int, job: str):
    db.x("UPDATE schedules SET last_run=? WHERE client_id=? AND job=?", (util.now(), client_id, job))
    await tracked(client_id, job, "schedule", lambda: BODIES[job](client_id))

# ---- system jobs
async def sys_verify():
    await tracked(None, "verify_sweep", "system", lambda: verify.sweep(shared()))
async def sys_sync():
    r = await asyncio.to_thread(sheets.sync_dirty)
    if r["rows"] or r["errors"]: log.info("sheet sync %s", r)
async def sys_tabs(): await asyncio.to_thread(sheets.ensure_all_tabs)
async def sys_backup():
    dest = config.DATA_DIR / "backups"; dest.mkdir(exist_ok=True)
    out = dest / f"seocrm-{datetime.now().strftime('%Y%m%d')}.db"
    src, dst = db.connect(), sqlite3.connect(out)
    try: src.backup(dst)                                                  # consistent online backup
    finally: src.close(); dst.close()
    for old in sorted(dest.glob("seocrm-*.db"))[:-7]: old.unlink()

def recover_after_crash():
    n1 = db.x("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (util.now(),))
    db.x("""UPDATE backlinks SET status='failed', retryable=1, next_retry_at=?, detail='interrupted before result was recorded'
            WHERE status='reserved'""", (util.now(),))
