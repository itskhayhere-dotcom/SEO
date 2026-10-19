-- ===== clients & scheduling =====
CREATE TABLE clients(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT 'IN',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','archived')),
  niche TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  contact_email TEXT NOT NULL DEFAULT '',
  anchors TEXT NOT NULL DEFAULT '[]',
  daily_cap INTEGER NOT NULL DEFAULT 25,
  max_spam INTEGER NOT NULL DEFAULT 40,
  min_da INTEGER NOT NULL DEFAULT 0,
  sheet_tab TEXT NOT NULL,
  sheet_status TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | error
  sheet_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE schedules(
  id INTEGER PRIMARY KEY,
  client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  job TEXT NOT NULL CHECK(job IN ('backlinks','audit','metrics')),
  cron TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run TEXT,
  UNIQUE(client_id, job)
);
CREATE TABLE runs(
  id INTEGER PRIMARY KEY,
  client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
  job TEXT NOT NULL, trigger TEXT NOT NULL,
  status TEXT NOT NULL,                            -- running | ok | error | interrupted
  started_at TEXT NOT NULL, finished_at TEXT, stats TEXT, error TEXT
);

-- ===== link graph (self-built crawl data) =====
CREATE TABLE domains(
  domain TEXT PRIMARY KEY, ip TEXT, tld TEXT, first_seen TEXT, last_crawled TEXT,
  sitemap_urls INTEGER, fresh_ratio REAL,
  da REAL, spam REAL, traffic REAL, scored_at TEXT, detail TEXT
);
CREATE TABLE pages(
  key TEXT PRIMARY KEY, url TEXT NOT NULL, domain TEXT NOT NULL, status INTEGER,
  fetched_at TEXT, lastmod TEXT, title TEXT, text_len INTEGER, ext_out INTEGER, kw_hits INTEGER, depth INTEGER
);
CREATE INDEX idx_pages_domain ON pages(domain);
CREATE TABLE links(
  id INTEGER PRIMARY KEY,
  src_key TEXT NOT NULL, src_url TEXT NOT NULL, src_domain TEXT NOT NULL,
  dst_key TEXT NOT NULL, dst_url TEXT NOT NULL, dst_domain TEXT NOT NULL,
  anchor TEXT, nofollow INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  UNIQUE(src_key, dst_key)
);
CREATE INDEX idx_links_dst ON links(dst_domain);
CREATE INDEX idx_links_src ON links(src_domain);

-- ===== metric history =====
CREATE TABLE domain_metrics(
  id INTEGER PRIMARY KEY, domain TEXT NOT NULL, da REAL, spam REAL, traffic REAL,
  graph_nodes INTEGER, computed_at TEXT NOT NULL, detail TEXT
);
CREATE INDEX idx_dm ON domain_metrics(domain, computed_at);
CREATE TABLE page_metrics(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL,
  pa REAL, computed_at TEXT NOT NULL
);
CREATE INDEX idx_pm ON page_metrics(key, computed_at);

-- ===== backlink targets =====
CREATE TABLE targets(
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL, norm_url TEXT NOT NULL UNIQUE, domain TEXT NOT NULL,
  country TEXT, country_score REAL, is_global INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,                              -- directory | guest_post | forum | comment
  source TEXT, needs_account INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',              -- new | ok | dead | blocked
  da REAL, pa REAL, spam REAL, traffic REAL, rank_score REAL,
  discovered_at TEXT NOT NULL, last_scored TEXT, note TEXT
);
CREATE INDEX idx_targets_pick ON targets(status, country, rank_score);

-- ===== backlinks (the de-duplication ledger) =====
CREATE TABLE backlinks(
  id INTEGER PRIMARY KEY,
  client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  target_id INTEGER REFERENCES targets(id) ON DELETE SET NULL,
  target_url TEXT NOT NULL, target_norm TEXT NOT NULL,
  anchor TEXT NOT NULL, dest_url TEXT NOT NULL,
  norm_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',         -- reserved|submitted|verified|failed|needs_manual
  verification TEXT NOT NULL DEFAULT 'pending',    -- pending|live|live_noindex|not_found|error
  retryable INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT,
  verify_attempts INTEGER NOT NULL DEFAULT 0, next_verify_at TEXT,
  listing_url TEXT, rel TEXT, http_status INTEGER, detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  submitted_at TEXT, verified_at TEXT,
  sheet_dirty INTEGER NOT NULL DEFAULT 1, rev INTEGER NOT NULL DEFAULT 0,
  UNIQUE(client_id, norm_hash)                     -- hard guarantee: no duplicate per client
);
CREATE INDEX idx_bl_client ON backlinks(client_id, created_at);
CREATE INDEX idx_bl_target ON backlinks(client_id, target_norm);
CREATE INDEX idx_bl_dirty ON backlinks(sheet_dirty);
-- any meaningful change bumps rev and marks the row for sheet sync
CREATE TRIGGER backlinks_rev AFTER UPDATE OF status, verification, rel, listing_url, detail, verified_at, anchor, submitted_at ON backlinks
BEGIN
  UPDATE backlinks SET rev = rev + 1, sheet_dirty = 1 WHERE id = NEW.id;
END;

-- ===== audits =====
CREATE TABLE audits(
  id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL, finished_at TEXT, pages INTEGER DEFAULT 0, score INTEGER, summary TEXT
);
CREATE TABLE audit_pages(
  id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
  url TEXT NOT NULL, status INTEGER, depth INTEGER, ttfb_ms INTEGER, bytes INTEGER, title TEXT, metrics TEXT
);
CREATE TABLE audit_issues(
  id INTEGER PRIMARY KEY, audit_id INTEGER NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
  severity TEXT NOT NULL, code TEXT NOT NULL, url TEXT, detail TEXT
);
CREATE INDEX idx_ai ON audit_issues(audit_id, severity);
