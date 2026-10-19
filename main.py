"""FastAPI app: routes, auth gate, CSV export, JSON for charts."""
from __future__ import annotations
import csv, io, json, logging, sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from . import auth, config, db, jobs, scheduler, sheets, util, discovery
from .crawler import shared

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
HERE = Path(__file__).parent

@asynccontextmanager
async def lifespan(app):
    if not config.APP_PASSWORD_HASH:
        raise SystemExit("APP_PASSWORD_HASH is not set. Run: python -m app.hashpw  and put the line in .env")
    db.migrate()
    scheduler.start()
    yield
    scheduler.stop()
    await shared().close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="seocrm", max_age=12 * 3600,
                   same_site="strict", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
T = Jinja2Templates(directory=HERE / "templates")
T.env.filters["fromjson"] = lambda v: json.loads(v or "{}")

class NotAuthed(Exception): ...
@app.exception_handler(NotAuthed)
async def _na(request, exc): return RedirectResponse("/login", 303)

def login_required(request: Request):
    if not request.session.get("ok"):
        raise NotAuthed()

async def csrf(request: Request, csrf_token: str = Form("")):
    if not auth.csrf_ok(request.session, csrf_token):
        raise HTTPException(403, "bad CSRF token")

SEC = [Depends(login_required)]
SEC_POST = [Depends(login_required), Depends(csrf)]

def render(request, name, **ctx):
    flash = request.session.pop("flash", None)
    return T.TemplateResponse(request, name, {"csrf": auth.csrf_token(request.session), "flash": flash,
                                              "countries": discovery.COUNTRIES, "tzname": config.TIMEZONE, **ctx})

def flash(request, msg): request.session["flash"] = msg
def back(url): return RedirectResponse(url, 303)

@app.get("/healthz")
async def health(): return {"ok": True}

# ---------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request): return render(request, "login.html")

@app.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "?"
    if auth.throttled(ip):
        flash(request, "Too many attempts - wait 5 minutes."); return back("/login")
    if auth.verify_password(password):
        request.session.clear(); request.session["ok"] = True
        return back("/")
    auth.record_fail(ip); flash(request, "Wrong password."); return back("/login")

@app.post("/logout", dependencies=SEC_POST)
async def logout(request: Request):
    request.session.clear(); return back("/login")

# ---------------------------------------------------------------- dashboard
def client_card(c):
    dm = db.one("SELECT * FROM domain_metrics WHERE domain=? ORDER BY id DESC LIMIT 1", (c["domain"],))
    pm = db.one("SELECT pa FROM page_metrics WHERE key=? ORDER BY id DESC LIMIT 1", (__import__("app.dedupe", fromlist=["x"]).normalize_url(c["url"]),))
    s, e = util.today_range_utc()
    cnt = lambda w, a=(): db.one(f"SELECT COUNT(*) n FROM backlinks WHERE client_id=? {w}", (c["id"], *a))["n"]
    au = db.one("SELECT * FROM audits WHERE client_id=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (c["id"],))
    return dict(c=c, da=dm and dm["da"], spam=dm and dm["spam"], pa=pm and pm["pa"], traffic=dm and dm["traffic"],
                today=cnt("AND submitted_at>=? AND submitted_at<? AND status IN ('submitted','verified')", (s, e)),
                total=cnt("AND status IN ('submitted','verified')"), verified=cnt("AND status='verified'"),
                manual=cnt("AND status='needs_manual'"), failed=cnt("AND status='failed'"),
                audit=au, audit_counts=json.loads(au["summary"]) if au and au["summary"] else {})

@app.get("/", response_class=HTMLResponse, dependencies=SEC)
async def dashboard(request: Request):
    cards = [client_card(c) for c in db.q("SELECT * FROM clients WHERE status!='archived' ORDER BY name")]
    runs = db.q("SELECT r.*, c.name cname FROM runs r LEFT JOIN clients c ON c.id=r.client_id ORDER BY r.id DESC LIMIT 8")
    return render(request, "dashboard.html", cards=cards, runs=runs, page="dashboard")

# ---------------------------------------------------------------- clients
def _form_client(name, url, country, status, niche, description, contact_email, anchors, daily_cap, max_spam, min_da):
    if not name.strip() or not url.strip():
        raise HTTPException(422, "name and URL are required")
    url = util.ensure_url(url)
    return dict(name=name.strip(), url=url, domain=util.registered_domain(url), country=country.upper(), status=status,
                niche=niche.strip(), description=description.strip(), contact_email=contact_email.strip(),
                anchors=json.dumps([a.strip() for a in anchors.splitlines() if a.strip()]),
                daily_cap=max(1, min(daily_cap, 200)), max_spam=max(0, min(max_spam, 100)), min_da=max(0, min(min_da, 100)))

@app.get("/clients", response_class=HTMLResponse, dependencies=SEC)
async def clients(request: Request):
    return render(request, "clients.html", rows=db.q("SELECT * FROM clients ORDER BY name"), page="clients")

@app.post("/clients", dependencies=SEC_POST)
async def client_add(request: Request, name: str = Form(""), url: str = Form(""), country: str = Form("IN"), status: str = Form("active"),
                     niche: str = Form(""), description: str = Form(""), contact_email: str = Form(""), anchors: str = Form(""),
                     daily_cap: int = Form(25), max_spam: int = Form(40), min_da: int = Form(0)):
    d = _form_client(name, url, country, status, niche, description, contact_email, anchors, daily_cap, max_spam, min_da)
    d["sheet_tab"] = util.sanitize_tab(d["name"])
    try:
        cid = _insert_client(d)
    except sqlite3.IntegrityError:
        flash(request, "A client with that name already exists."); return back("/clients")
    for job, cron in scheduler.DEFAULTS.items():                  # schedules exist but start disabled until you enable them
        db.x("INSERT INTO schedules(client_id,job,cron,enabled) VALUES (?,?,?,0)", (cid, job, cron))
    await __import__("asyncio").to_thread(sheets.ensure_client_tab, cid)   # creates the tab; retried by sys:tabs if it fails
    c = db.one("SELECT sheet_status, sheet_error FROM clients WHERE id=?", (cid,))
    flash(request, "Client added." + ("" if c["sheet_status"] == "ok" or config.SHEETS_BACKEND == "none"
                                       else f" Sheet tab will be retried automatically ({c['sheet_error']})"))
    return back(f"/clients/{cid}")

def _insert_client(d):
    cols = ",".join(d); ph = ",".join("?" * len(d))
    return db.x(f"INSERT INTO clients({cols}) VALUES ({ph})", tuple(d.values()))

@app.get("/clients/{cid}", response_class=HTMLResponse, dependencies=SEC)
async def client_page(request: Request, cid: int):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    if not c: raise HTTPException(404)
    card = client_card(c)
    return render(request, "client.html", c=c, card=card, anchors="\n".join(json.loads(c["anchors"])), page="clients",
                  recent=db.q("SELECT * FROM backlinks WHERE client_id=? ORDER BY id DESC LIMIT 10", (cid,)),
                  runs=db.q("SELECT * FROM runs WHERE client_id=? ORDER BY id DESC LIMIT 8", (cid,)),
                  running=[j for j in jobs.BODIES if jobs.is_running(cid, j)],
                  pool=db.one("SELECT COUNT(*) n FROM targets WHERE status='ok' AND country=?", (c["country"],))["n"],
                  nodes=db.one("SELECT COUNT(*) n FROM domains")["n"])

@app.post("/clients/{cid}/edit", dependencies=SEC_POST)
async def client_edit(request: Request, cid: int, name: str = Form(""), url: str = Form(""), country: str = Form("IN"), status: str = Form("active"),
                      niche: str = Form(""), description: str = Form(""), contact_email: str = Form(""), anchors: str = Form(""),
                      daily_cap: int = Form(25), max_spam: int = Form(40), min_da: int = Form(0)):
    old = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    if not old: raise HTTPException(404)
    d = _form_client(name, url, country, status, niche, description, contact_email, anchors, daily_cap, max_spam, min_da)
    new_tab = util.sanitize_tab(d["name"]) if d["name"] != old["name"] else old["sheet_tab"]
    try:
        db.x(f"UPDATE clients SET {','.join(k + '=?' for k in d)}, sheet_tab=? WHERE id=?", (*d.values(), new_tab, cid))
    except sqlite3.IntegrityError:
        flash(request, "Another client already uses that name."); return back(f"/clients/{cid}")
    if new_tab != old["sheet_tab"]:
        try:
            await __import__("asyncio").to_thread(sheets.backend().rename_tab, old["sheet_tab"], new_tab)
        except Exception as e:
            db.x("UPDATE clients SET sheet_status='error', sheet_error=? WHERE id=?", (str(e)[:300], cid))
    scheduler.reload_schedules()
    flash(request, "Saved."); return back(f"/clients/{cid}")

@app.post("/clients/{cid}/delete", dependencies=SEC_POST)
async def client_delete(request: Request, cid: int, confirm: str = Form(""), delete_tab: str = Form("")):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    if not c: raise HTTPException(404)
    if confirm.strip() != c["name"]:
        flash(request, "Type the client name exactly to confirm deletion."); return back(f"/clients/{cid}")
    if delete_tab:
        try: await __import__("asyncio").to_thread(sheets.backend().delete_tab, c["sheet_tab"])
        except Exception as e: flash(request, f"Client deleted; sheet tab not removed: {e}")
    db.x("DELETE FROM clients WHERE id=?", (cid,))
    scheduler.reload_schedules()
    return back("/clients")

@app.post("/clients/{cid}/run/{job}", dependencies=SEC_POST)
async def run_job(request: Request, cid: int, job: str):
    if job not in jobs.BODIES or not db.one("SELECT 1 FROM clients WHERE id=?", (cid,)): raise HTTPException(404)
    flash(request, f"{job} started." if jobs.launch(cid, job) else f"{job} is already running.")
    return back(request.headers.get("referer") or f"/clients/{cid}")

@app.get("/api/clients/{cid}/history", dependencies=SEC)
async def history(cid: int):
    c = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    if not c: raise HTTPException(404)
    from .dedupe import normalize_url
    dm = db.q("SELECT computed_at t, da, spam, traffic FROM domain_metrics WHERE domain=? ORDER BY id", (c["domain"],))
    pm = db.q("SELECT computed_at t, pa FROM page_metrics WHERE key=? ORDER BY id", (normalize_url(c["url"]),))
    return {"da": [[r["t"], r["da"]] for r in dm], "spam": [[r["t"], r["spam"]] for r in dm],
            "traffic": [[r["t"], r["traffic"]] for r in dm], "pa": [[r["t"], r["pa"]] for r in pm]}

# ---------------------------------------------------------------- backlinks table + export
def _bl_query(client_id, status, q, d_from, d_to):
    w, a = ["1=1"], []
    if client_id: w.append("b.client_id=?"); a.append(client_id)
    if status: w.append("b.status=?"); a.append(status)
    if q: w.append("(b.target_url LIKE ? OR b.anchor LIKE ?)"); a += [f"%{q}%", f"%{q}%"]
    if d_from: w.append("date(b.created_at)>=date(?)"); a.append(d_from)
    if d_to: w.append("date(b.created_at)<=date(?)"); a.append(d_to)
    return " AND ".join(w), a

@app.get("/backlinks", response_class=HTMLResponse, dependencies=SEC)
async def backlinks(request: Request, client_id: int = 0, status: str = "", q: str = "", date_from: str = "", date_to: str = "", p: int = 1):
    w, a = _bl_query(client_id, status, q, date_from, date_to)
    total = db.one(f"SELECT COUNT(*) n FROM backlinks b WHERE {w}", a)["n"]
    rows = db.q(f"""SELECT b.*, c.name cname, t.spam, t.da FROM backlinks b JOIN clients c ON c.id=b.client_id
                    LEFT JOIN targets t ON t.id=b.target_id WHERE {w} ORDER BY b.id DESC LIMIT 100 OFFSET ?""", (*a, (max(p, 1) - 1) * 100))
    qs = f"client_id={client_id}&status={quote(status)}&q={quote(q)}&date_from={date_from}&date_to={date_to}"
    return render(request, "backlinks.html", rows=rows, total=total, p=p, qs=qs, f=dict(client_id=client_id, status=status, q=q, date_from=date_from, date_to=date_to),
                  clients=db.q("SELECT id, name FROM clients ORDER BY name"), page="backlinks")

@app.get("/backlinks.csv", dependencies=SEC)
async def backlinks_csv(client_id: int = 0, status: str = "", q: str = "", date_from: str = "", date_to: str = ""):
    w, a = _bl_query(client_id, status, q, date_from, date_to)
    rows = db.q(f"""SELECT b.id, c.name client, b.created_at, b.target_url, b.anchor, b.dest_url, b.status, b.verification, b.rel,
                    t.spam target_spam, t.da target_da, b.listing_url, b.verified_at, b.detail
                    FROM backlinks b JOIN clients c ON c.id=b.client_id LEFT JOIN targets t ON t.id=b.target_id WHERE {w} ORDER BY b.id""", a)
    buf = io.StringIO(); wr = csv.writer(buf)
    wr.writerow(rows[0].keys() if rows else ["id"])
    for r in rows: wr.writerow([("'" + str(v) if isinstance(v, str) and v[:1] in "=+-@" else v) for v in tuple(r)])   # CSV-injection guard
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=backlinks.csv"})

# ---------------------------------------------------------------- audits
@app.get("/audits", response_class=HTMLResponse, dependencies=SEC)
async def audits(request: Request):
    rows = db.q("SELECT a.*, c.name cname FROM audits a JOIN clients c ON c.id=a.client_id ORDER BY a.id DESC LIMIT 100")
    return render(request, "audits.html", rows=[dict(r, counts=json.loads(r["summary"] or "{}")) for r in rows], page="audits",
                  clients=db.q("SELECT id, name FROM clients WHERE status='active' ORDER BY name"))

@app.get("/audits/{aid}", response_class=HTMLResponse, dependencies=SEC)
async def audit_report(request: Request, aid: int, sev: str = ""):
    a = db.one("SELECT a.*, c.name cname FROM audits a JOIN clients c ON c.id=a.client_id WHERE a.id=?", (aid,))
    if not a: raise HTTPException(404)
    w, args = ("AND severity=?", [sev]) if sev else ("", [])
    issues = db.q(f"""SELECT * FROM audit_issues WHERE audit_id=? {w} ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                      WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, code, url LIMIT 1000""", (aid, *args))
    return render(request, "audit.html", a=a, counts=json.loads(a["summary"] or "{}"), issues=issues, sev=sev, page="audits",
                  pages=db.q("SELECT * FROM audit_pages WHERE audit_id=? ORDER BY depth, url LIMIT 300", (aid,)))

# ---------------------------------------------------------------- scheduler
@app.get("/scheduler", response_class=HTMLResponse, dependencies=SEC)
async def scheduler_page(request: Request):
    rows = db.q("SELECT s.*, c.name cname FROM schedules s JOIN clients c ON c.id=s.client_id ORDER BY c.name, s.job")
    rows = [dict(r, next=scheduler.next_run(r["client_id"], r["job"]), running=jobs.is_running(r["client_id"], r["job"])) for r in rows]
    runs = db.q("SELECT r.*, c.name cname FROM runs r LEFT JOIN clients c ON c.id=r.client_id ORDER BY r.id DESC LIMIT 25")
    return render(request, "scheduler.html", rows=rows, runs=runs, page="scheduler",
                  sheet=dict(backend=config.SHEETS_BACKEND, dirty=db.one("SELECT COUNT(*) n FROM backlinks WHERE sheet_dirty=1 AND status!='reserved'")["n"]))

@app.post("/scheduler", dependencies=SEC_POST)
async def scheduler_save(request: Request, schedule_id: int = Form(...), cron: str = Form(...), enabled: str = Form("")):
    cron = " ".join(cron.split())
    if not scheduler.valid_cron(cron):
        flash(request, f"'{cron}' is not a valid 5-field cron expression."); return back("/scheduler")
    db.x("UPDATE schedules SET cron=?, enabled=? WHERE id=?", (cron, 1 if enabled else 0, schedule_id))
    scheduler.reload_schedules()
    flash(request, "Schedule saved."); return back("/scheduler")
