import pytest
from app import db, agent, dedupe

@pytest.fixture(scope="module")
def client():
    db.migrate()
    db.x("INSERT INTO clients(id,name,url,domain,country,sheet_tab,max_spam,min_da) VALUES (3,'C3','https://c3.com','c3.com','OM','C3',30,10)")
    def t(url, dom, country, spam, da, rank, kind="directory", glob=0, acct=0, status="ok"):
        db.x("""INSERT INTO targets(url,norm_url,domain,country,is_global,kind,needs_account,status,da,spam,rank_score,discovered_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""", (url, dedupe.normalize_url(url), dom, country, glob, kind, acct, status, da, spam, rank))
    t("https://good.om/submit", "good.om", "OM", 5, 40, 90)
    t("https://better.om/submit", "better.om", "OM", 3, 50, 95)
    t("https://spammy.om/submit", "spammy.om", "OM", 80, 60, 99)        # spam above client's max
    t("https://lowda.om/submit", "lowda.om", "OM", 5, 2, 99)            # DA below min
    t("https://india.in/submit", "india.in", "IN", 5, 60, 99)           # wrong country
    t("https://world.com/submit", "world.com", "", 5, 40, 80, glob=1)   # global ok
    t("https://acct.om/join", "acct.om", "OM", 5, 40, 99, acct=1)       # needs account
    t("https://blog.om/post", "blog.om", "OM", 5, 40, 99, kind="comment")  # manual-only kind
    return db.one("SELECT * FROM clients WHERE id=3")

def test_eligibility_filters_and_ranking(client):
    urls = [r["url"] for r in agent.eligible(client, 20)]
    assert urls == ["https://better.om/submit", "https://good.om/submit", "https://world.com/submit"]

def test_used_targets_and_domains_are_excluded(client):
    t = db.one("SELECT * FROM targets WHERE url='https://better.om/submit'")
    assert dedupe.reserve(3, t["id"], t["url"], "C3", client["url"])
    urls = [r["url"] for r in agent.eligible(client, 20)]
    assert "https://better.om/submit" not in urls and len(urls) == 2

def test_anchor_rotation_picks_least_used(client):
    db.x("UPDATE clients SET anchors='[\"A\",\"B\"]' WHERE id=3")
    c = db.one("SELECT * FROM clients WHERE id=3")
    db.x("UPDATE backlinks SET anchor='A' WHERE client_id=3")
    assert agent.pick_anchor(c) == "B"
