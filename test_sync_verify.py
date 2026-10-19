"""Sheets sync idempotency + verification, using a fake backend and the local test site on :8102 (see README 'Tests')."""
import asyncio, os, tempfile
import pytest
from app import db, sheets, dedupe, verify, crawler

class Fake:
    name = "fake"
    def __init__(self): self.tabs, self.fail = {}, False
    def ensure_tab(self, t): self.tabs.setdefault(t, {})
    def upsert_rows(self, t, rows):
        if self.fail: raise RuntimeError("quota")
        for r in rows: self.tabs[t][str(r[0])] = r

@pytest.fixture(scope="module")
def env():
    db.migrate()
    db.x("INSERT INTO clients(id,name,url,domain,sheet_tab,sheet_status) VALUES (2,'T2','http://localhost:8101/','localhost','T','ok')")
    f = Fake(); sheets._backend = f
    return f

def test_sync_is_idempotent_and_retries_after_failure(env):
    bid = dedupe.reserve(2, None, "http://x.com/a", "T", "http://localhost:8101/")
    assert sheets.sync_dirty()["rows"] == 0                         # 'reserved' rows are not synced
    db.x("UPDATE backlinks SET status='submitted', submitted_at=datetime('now') WHERE id=?", (bid,))
    env.fail = True; r = sheets.sync_dirty(); assert r["errors"] == 1
    assert db.one("SELECT sheet_dirty FROM backlinks WHERE id=?", (bid,))["sheet_dirty"] == 1   # stays dirty -> retried
    env.fail = False; assert sheets.sync_dirty()["rows"] == 1
    assert db.one("SELECT sheet_dirty FROM backlinks WHERE id=?", (bid,))["sheet_dirty"] == 0
    assert sheets.sync_dirty()["rows"] == 0                         # nothing re-sent
    db.x("UPDATE backlinks SET status='verified', verification='live' WHERE id=?", (bid,))     # trigger re-dirties it
    assert sheets.sync_dirty()["rows"] == 1 and len(env.tabs["T"]) == 1 and env.tabs["T"][str(bid)][5] == "verified"

def test_edit_during_sync_is_not_lost(env):
    bid = dedupe.reserve(2, None, "http://y.com/a", "T", "http://localhost:8101/")
    db.x("UPDATE backlinks SET status='submitted' WHERE id=?", (bid,))
    rows = db.q("SELECT * FROM backlinks WHERE id=?", (bid,))
    db.x("UPDATE backlinks SET detail='changed mid-sync' WHERE id=?", (bid,))                  # rev bumps after our read
    db.x("UPDATE backlinks SET sheet_dirty=0 WHERE id=? AND rev=?", (bid, rows[0]["rev"]))     # what sync_dirty does
    assert db.one("SELECT sheet_dirty FROM backlinks WHERE id=?", (bid,))["sheet_dirty"] == 1

def test_verify_finds_live_link():
    import urllib.request
    try: urllib.request.urlopen("http://127.0.0.1:8102/", timeout=2)
    except Exception: pytest.skip("test site :8102 not running")
    async def go():
        f = crawler.Fetcher()
        try: return await verify.check_live(f, ["http://127.0.0.1:8102/"], "http://localhost:8101/", "http://localhost:8101/")
        finally: await f.close()
    r = asyncio.run(go())
    assert r["verification"] == "live" and "index-ready" in r["detail"]
