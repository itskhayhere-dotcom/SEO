# SEO CRM - self-hosted, single-owner, zero paid APIs

One private dashboard for: **clients -> self-built DA/PA/Spam -> autonomous backlink agent -> site audit -> Google Sheet sync**.
Python 3.11+ · FastAPI · SQLite (WAL) · APScheduler · Playwright (Chromium) · server-rendered UI with zero JS dependencies.
No Moz/Ahrefs/Semrush/Majestic or any third-party metric API is called, anywhere.

## 1. Setup (local or a small VPS)

```bash
cd seocrm
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install google-auth-oauthlib                          # only if you choose SHEETS_BACKEND=oauth
playwright install chromium                               # needed for submissions + lab speed metrics
cp .env.example .env
python -m app.hashpw                                      # prompts for a password -> paste the printed line into .env
python run.py                                             # http://127.0.0.1:8000  (migrations run automatically)
```
Run it as a service (systemd/pm2/Docker) so the scheduler stays alive. For remote access put it behind HTTPS
(Caddy/nginx), set `COOKIE_SECURE=true`, and keep `HOST=127.0.0.1`. Never expose port 8000 directly.

Tests: `python -m pytest -q tests` (the verification test uses a throwaway local site; it skips itself if that isn't running).

## 2. Google Sheet setup (spreadsheet named **SEO** must already exist)
Pick **one** backend in `.env` (`SHEETS_BACKEND=`). All are free. Creating a Cloud project or service account does **not** require a
billing account or card; the Apps Script option needs no Google Cloud project at all.

**A. `apps_script` - simplest, no Cloud project.** Open the SEO sheet -> *Extensions -> Apps Script* -> paste `apps_script/Code.gs` ->
set `SECRET` to a long random string -> *Deploy -> New deployment -> Web app* (Execute as **Me**, access **Anyone**) -> copy the `/exec` URL.
`.env`: `SHEETS_BACKEND=apps_script`, `APPS_SCRIPT_URL=...`, `APPS_SCRIPT_SECRET=<same secret>`. The script is bound to the sheet, so no sheet ID is needed.
(The URL is only usable with the secret; treat both as passwords.)

**B. `service_account`.** console.cloud.google.com -> new project -> enable **Google Sheets API** and **Google Drive API** -> *IAM -> Service accounts ->
create -> Keys -> JSON* -> save as `secrets/service-account.json`. Open the SEO sheet -> **Share** -> add the service account's e-mail as *Editor*.
`.env`: `SHEETS_BACKEND=service_account` (+ optionally `SHEET_ID=` to skip the name lookup).

**C. `oauth`.** Same project/APIs, create an *OAuth client ID (Desktop app)*, then `python scripts/oauth_login.py client_secret.json`
(writes `secrets/oauth-token.json`). Add yourself as a test user on the consent screen; tokens for "Testing" apps expire after 7 days, so prefer A or B for unattended use.

### How sync works (correctness guarantees)
* Creating a client creates a tab named after it (illegal characters `[]*/\?:` replaced). If Google is unreachable, the client is still created,
  `sheet_status` shows `error`, and a job retries every 10 minutes until the tab exists. Renaming a client renames its tab; deleting keeps the tab unless you tick the box.
* Each backlink row's **column A is its permanent ID**. Sync *upserts by ID* (read column A once -> batch-update existing rows, append new ones), so retries, crashes or
  re-runs can never create duplicate rows.
* A DB trigger bumps `rev` and sets `sheet_dirty=1` on every status/verification/detail change. Sync marks a row clean only if `rev` is unchanged since it was read,
  so an edit that lands mid-sync is re-sent. Failures leave rows dirty -> retried every 2 minutes with exponential backoff on 429/5xx. Batched calls stay well inside Sheets quotas.
* Columns: ID · Date · Target URL · Anchor · Destination · Status · Verification · Rel · Target Spam · Target DA · Listing URL · Verified At · Notes.

## 3. Using it
1. **Clients -> Add**: name, URL, country, niche, contact e-mail (needed by most forms), anchor variants (one per line), daily cap, max spam, min DA.
2. Client page -> **Refresh DA/PA/Spam** (builds/extends the link graph, appends a history point), **Discover targets**, **Run backlink agent**, **Run audit**, **Verify now**.
3. **Scheduler**: per client and job (`backlinks`, `audit`, `metrics`) a 5-field cron in your `TIMEZONE`. New clients get sensible defaults, *disabled* until you switch them on.
4. **Backlinks**: filter by client/status/date/text, export CSV. **Audits**: severity-ranked reports.

## 4. Architecture
```
app/main.py        routes, auth gate, CSRF, CSV export      app/audit.py      site crawl + issue engine
app/auth.py        scrypt hash, session, login throttle     app/agent.py      daily run: pool -> reserve -> submit -> log
app/db.py          SQLite + migrations (app/migrations)     app/submitter.py  Playwright form filler (no captcha bypass)
app/crawler.py     polite fetcher (robots, pacing, SSRF)    app/verify.py     live / rel / index-ready check + retry backoff
app/linkgraph.py   page+link store, snowball crawl          app/dedupe.py     normalisation + reservation ledger
app/authority.py   PageRank DA/PA, spam heuristics          app/sheets.py     3 backends + idempotent sync
app/discovery.py   targets, country filter, scoring         app/scheduler.py  APScheduler from DB, catch-up, recovery
app/jobs.py        run tracking, locks, backup              apps_script/Code.gs  free Sheets bridge
```
Schema: `app/migrations/001_init.sql` (add new `NNN_*.sql` files; they apply once, in order, at startup).

### De-duplication (highest priority)
Identity = `sha256(normalize(target URL) | normalize(anchor) | normalize(destination))`, per client. Normalisation drops scheme, `www.`, default ports, fragments, tracking params
(`utm_*`, `gclid`, ...), sorts the query, collapses slashes, strips trailing slash/`index.html`; anchors are NFKC-casefolded, whitespace-collapsed, edge-punctuation-stripped.
Three layers: pre-check inside a `BEGIN IMMEDIATE` transaction -> `UNIQUE(client_id, norm_hash)` -> (default) `STRICT_TARGET_DEDUPE`: a target URL is used once per client whatever the anchor
(and `ALLOW_SAME_DOMAIN=false` allows one backlink per referring domain). The row is **reserved before** submitting, so a crash can never double-submit; interrupted rows become
retryable *on the same row*. Covered by tests including 12 concurrent threads racing for one slot.

### Scheduler reliability
Jobs are rebuilt from the `schedules` table at startup and after each edit (the UI and the DB can't drift). `max_instances=1`, `coalesce=True`, 6 h misfire grace, an in-process
per-(client, job) lock so a button press never overlaps a cron run, run history (`runs`) with errors, crash recovery (`running` -> `interrupted`), **catch-up** of a run missed while the app was
down (`CATCHUP_ON_START`), and a nightly consistent SQLite backup (7 kept in `data/backups`).

## 5. Politeness & rate limiting
* Identifies itself with `USER_AGENT` (put a real contact URL/e-mail in it) and **obeys robots.txt** for every third-party fetch, including `Crawl-delay`; a robots 5xx is treated as "disallow".
* Per-host pacing: `CRAWL_DELAY_SEC` (default 2 s + jitter) with one request at a time per host; global concurrency `GLOBAL_CONCURRENCY=8`; 429/503 honour `Retry-After`; 2 MB response cap; max 8 redirects.
* Your own site is crawled faster (`OWN_SITE_DELAY_SEC`) and robots-disallowed URLs are *reported*, not hidden. External link-checking uses HEAD with the same pacing.
* Submissions are spaced 20-60 s apart (jittered), capped by `daily_cap`, and run at most `RUN_MAX_MINUTES`. One backlink per referring domain per client by default.
* SSRF guard: hosts resolving to private/loopback/link-local addresses are refused (so a malicious redirect can't reach your LAN). `ALLOW_PRIVATE_HOSTS=true` is for testing only.

## 6. Tradeoffs - read these before trusting a number
**DA / PA are not Moz's.** They are *relative to the graph you've crawled*: damped PageRank (d=0.85) over your own link graph, log-scaled so the strongest domain in it = 100.
Without crawling the whole web nobody can reproduce Moz/Ahrefs values; here the numbers are consistent and comparable *within this tool*, and get steadier as the graph grows
(each metrics run snowballs your site + its most-linked neighbours; verified backlinks and discovered targets feed it too). The graph size is shown next to DA. Inbound links you
haven't crawled are invisible to it - optional SearXNG queries for `"yourdomain.com" -site:yourdomain.com` partly close that gap.

**Spam score is a heuristic, not Google's view.** Weighted blend of: link-velocity burstiness (page Last-Modified, else first-seen; needs >=5 links over >3 days), anchor concentration/money-keyword share,
neighbourhood quality (spam of linking/linked domains, two-pass), TLD risk + /24 hosting clustering (PBN footprint), thin/outlinky/spam-keyword content. Components with no data use a neutral
value and lower the stored `confidence` (see `domains.detail` JSON). Young or barely-crawled domains therefore score near-neutral rather than "clean".

**"Traffic" is a proxy** (sitemap size, sitemap freshness, referring-domain count, DA) - there is no free source of real traffic. Ranking = 0.35·DA + 0.20·traffic proxy + 0.45·(100 - spam).

**Discovery needs a search source.** Scraping Google/Bing breaks their terms and gets blocked, so discovery uses (1) a **self-hosted SearXNG** (`docker run -d -p 8888:8080 searxng/searxng`, enable the `json` format in its
settings) and (2) seed list pages in `seeds/seeds.txt`. With neither, the pool stays empty and the agent reports a shortfall instead of faking activity. Country filtering combines ccTLD, `lang`
attribute, phone prefix and geo keywords (table in `discovery.py`); worldwide-friendly sites are accepted when `INCLUDE_GLOBAL_TARGETS=true`.

**20/day is a ceiling you earn, not a promise.** The agent submits until the cap or the pool runs out, tops the pool up automatically, and records `shortfall`. Realistic limits:
* **CAPTCHAs, account creation, e-mail confirmation: not automated and never bypassed.** Those targets get `needs_manual` with the reason (visible, filterable, synced to the sheet).
* **Moderation:** most directories approve in days. Status is `submitted` until a later crawl finds the link (verification retries at 2 h, 1 d, 3 d, 7 d, 14 d, then `failed`). Daily cap counts *submissions*, not approvals.
* **Blog-comment targets are discovered but manual-only.** Auto-posting link comments is the textbook link-spam pattern: it harms the sites you post on, the links are nofollow/moderated almost everywhere, and a spammy
  footprint can hurt your client. The submitter refuses `comment` and `forum` kinds. Directory and guest-post (contact-form pitch) targets are automated.
* Generic form-filling is heuristic (visible fields only - honeypots skipped; unmapped required fields abort cleanly). Expect a meaningful failure rate on unusual forms; failures are logged and retried (`MAX_SUBMIT_ATTEMPTS`).

**"Indexed-ready", not "indexed".** Verification confirms the link is in the HTML, records `rel` (nofollow/ugc/sponsored), and checks 200 status, no `noindex` (meta/header), robots.txt allows Googlebot, canonical not pointing elsewhere.
Actual index status needs Google Search Console, which is outside this tool.

**Quality, not just volume.** Mass low-value directory links can do nothing or hurt. The quality gate (`max_spam`, `min_da`, country match, one-per-domain) is on by default - tighten it rather than loosening it to hit a number.
Check each client's niche suits the targets you allow, and respect each site's terms.

**Audit speed metrics** are local: TTFB, HTML weight, compression, render-blocking scripts from HTTP; load/LCP/request count from headless Chromium on `AUDIT_BROWSER_SAMPLES` pages (lab data on your machine/network, not field data).

## 7. Security notes
Single password (scrypt), signed same-site-strict session cookie (12 h), CSRF token on every POST, 5-attempts/5-min login throttle, CSV export neutralises formula injection, no public API docs, binds to localhost.
Keep `.env`, `secrets/` and `data/` out of version control (`.gitignore` provided). Back up `data/seocrm.db` (nightly copies are in `data/backups`).

## 8. Troubleshooting
* *Tab not created / `sheet_status=error`*: the message is on the client page; fix sharing/credentials - it retries every 10 min.
* *Submissions all `failed: Chromium missing`*: run `playwright install chromium` (on Linux also `playwright install-deps`).
* *Pool is 0*: set `SEARXNG_URL` or add seeds, then **Discover targets**; check `max_spam` isn't too strict for a young graph.
* *Scores look odd on first run*: the graph is tiny; run metrics a few times and after discovery. DA of 100 just means "strongest in this graph".
