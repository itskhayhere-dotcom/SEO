import sqlite3, threading
import pytest
from app import db, dedupe, authority

@pytest.fixture(autouse=True, scope="module")
def _db():
    db.migrate()
    db.x("INSERT INTO clients(id,name,url,domain,sheet_tab) VALUES (1,'T','https://me.com','me.com','T')")

def test_normalisation_collapses_variants():
    a = dedupe.link_hash("http://www.Example.com/a/index.html?utm_source=x", "Best  Site!", "https://me.com/")
    b = dedupe.link_hash("https://example.com/a", "best site", "me.com")
    assert a == b
    assert a != dedupe.link_hash("https://example.com/a", "other anchor", "me.com")

def test_reserve_blocks_duplicates_and_target_reuse():
    assert dedupe.reserve(1, None, "https://dir.com/submit", "Acme", "https://me.com") is not None
    assert dedupe.reserve(1, None, "http://www.dir.com/submit/", "acme", "me.com") is None      # same hash
    assert dedupe.reserve(1, None, "https://dir.com/submit?utm_x=1", "Different", "me.com") is None  # strict target
    assert dedupe.reserve(1, None, "https://dir.com/submit", "Different", "me.com", strict=False) is not None

def test_concurrent_reservations_yield_one_row():
    res = []
    def go(): res.append(dedupe.reserve(1, None, "https://race.com/x", "Race", "https://me.com"))
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(r is not None for r in res) == 1
    assert db.one("SELECT COUNT(*) c FROM backlinks WHERE target_norm='race.com/x'")["c"] == 1

def test_db_constraint_is_last_line_of_defence():
    h = dedupe.link_hash("https://direct.com/", "A", "me.com")
    db.x("INSERT INTO backlinks(client_id,target_url,target_norm,anchor,dest_url,norm_hash) VALUES (1,'https://direct.com/','direct.com/','A','me.com',?)", (h,))
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO backlinks(client_id,target_url,target_norm,anchor,dest_url,norm_hash) VALUES (1,'https://direct.com/','direct.com/','A','me.com',?)", (h,))

def test_pagerank_sums_to_one_and_ranks_hub():
    g = {"a": {"hub": 1}, "b": {"hub": 1}, "c": {"hub": 1}, "hub": {"a": 1}}
    pr = authority.pagerank(g)
    assert abs(sum(pr.values()) - 1) < 1e-9
    assert max(pr, key=pr.get) == "hub"
    s = authority.scale_100(pr)
    assert s["hub"] == 100 and all(0 <= v <= 100 for v in s.values())

def test_spam_components_respond_to_patterns():
    assert authority.comp_anchor(["buy cheap pills"] * 9 + ["x"], "foo") > 0.5
    assert authority.comp_anchor(["Acme", "John", "this post", "click", "read more", "site", "review", "blog"], "acme") < 0.3
    assert authority.comp_tld_host("xyz", []) > authority.comp_tld_host("com", [])
    assert authority.comp_tld_host("com", ["5.5.5.%d" % i for i in range(8)]) > authority.comp_tld_host("com", ["%d.1.1.1" % i for i in range(8)])
