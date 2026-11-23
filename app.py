"""BCC-UERS: whole enrollment system.

Combines the 2009 Cavite Maritime Institute study (student records, subject and schedule
viewing, advising, fee assessment, payment choice, reports) with the BCC-UERS additions
(unified registration, online documents, registrar review, signed QR verification,
dashboard, audit log, privacy controls).

Flask + SQLite. Routes for the academic side live in academics.py.
"""
import hmac
import hashlib
import io
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from db import get_db_connection, PG_SCHEMA, DBAdapter

import qrcode
from flask import (Flask, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, session, url_for, make_response)

DOCS = ["Report card or TOR", "Valid ID", "Birth certificate", "2x2 photo"]
PROGRAMS = ["BEED", "BSED", "BSBA-MM", "BSEntrep", "BSOA", "BSIS"]
CAMPUSES = ["Taculing", "Sum-ag"]
PLANS = {"full": "Pay in full", "installment": "Two installments (half first)"}
METHODS = {"cashier": "Cashier at BCC", "bank": "Bank deposit", "ewallet": "E-wallet"}
MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Tuition per unit is the rate BCC publishes on its About page; the registrar can change it.
DEFAULT_SETTINGS = {"school_year": "2027-2028", "semester": "1", "tuition_per_unit": "30",
                    "max_units": "24", "sample_data": "0"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL, program TEXT NOT NULL, campus TEXT NOT NULL,
  year_level INTEGER NOT NULL DEFAULT 1, consent INTEGER NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id),
  doc_type TEXT NOT NULL, original TEXT, stored TEXT, status TEXT NOT NULL DEFAULT 'submitted',
  note TEXT DEFAULT '', updated TEXT NOT NULL, UNIQUE(student_id, doc_type));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ref TEXT, ok INTEGER NOT NULL, ms REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  units INTEGER NOT NULL, program TEXT NOT NULL, year_level INTEGER NOT NULL, semester TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER NOT NULL REFERENCES subjects(id),
  section_code TEXT NOT NULL, days TEXT NOT NULL, time_start TEXT NOT NULL, time_end TEXT NOT NULL,
  room TEXT NOT NULL, capacity INTEGER NOT NULL, UNIQUE(subject_id, section_code));
CREATE TABLE IF NOT EXISTS enrollments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id),
  section_id INTEGER NOT NULL REFERENCES sections(id), UNIQUE(student_id, section_id));
CREATE TABLE IF NOT EXISTS advising(
  student_id INTEGER PRIMARY KEY REFERENCES students(id), status TEXT NOT NULL,
  note TEXT DEFAULT '', updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fees(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payment_plans(
  student_id INTEGER PRIMARY KEY REFERENCES students(id), plan TEXT NOT NULL,
  method TEXT NOT NULL, chosen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id),
  amount REAL NOT NULL, method TEXT NOT NULL, note TEXT DEFAULT '', ts TEXT NOT NULL);
"""


# ---------- basic helpers ----------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def peso(x):
    return f"\u20b1{float(x):,.2f}"


def get_db():
    if "db" not in g:
        g.db = get_db_connection(current_app.config)
    return g.db


def audit(actor, action):
    get_db().execute("INSERT INTO audit(ts,actor,action) VALUES(?,?,?)", (now(), actor, action))
    get_db().commit()


def setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def set_setting(key, value):
    get_db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))
    get_db().commit()


def student_by_ref(ref):
    return get_db().execute("SELECT * FROM students WHERE ref=?", (ref,)).fetchone()


def validate_student(f, need_consent=True, check_email=True):
    name = f.get("name", "").strip()
    email = f.get("email", "").strip().lower()
    year = f.get("year_level", "1")
    errs = []
    if not 2 <= len(name) <= 80:
        errs.append("Enter the full name (2 to 80 characters).")
    if check_email and (not EMAIL.match(email) or len(email) > 80):
        errs.append("Enter a valid email address.")
    if f.get("program") not in PROGRAMS or f.get("campus") not in CAMPUSES:
        errs.append("Choose a program and a campus.")
    if year not in ("1", "2", "3", "4"):
        errs.append("Choose a year level from 1 to 4.")
    if need_consent and f.get("consent") != "on":
        errs.append("Consent to the data privacy notice is required.")
    if check_email and not errs and get_db().execute("SELECT 1 FROM students WHERE email=?", (email,)).fetchone():
        errs.append("This email is already registered. Use 'Check my status'.")
    clean = {"name": name, "email": email, "program": f.get("program"), "campus": f.get("campus"),
             "year_level": int(year) if year in ("1", "2", "3", "4") else 1}
    return errs, clean


def insert_student(c, actor=None, action="registered"):
    db = get_db()
    cur = db.execute("INSERT INTO students(name,email,program,campus,year_level,consent,created) VALUES(?,?,?,?,?,1,?)",
                     (c["name"], c["email"], c["program"], c["campus"], c["year_level"], now()))
    student_id = cur.lastrowid
    if student_id is None:
        inserted = cur.fetchone()
        student_id = inserted["id"] if inserted else None
    if student_id is None:
        db.rollback()
        raise RuntimeError("Student registration did not return a valid record ID.")
    ref = f"BCC-2027-{student_id:04d}"
    db.execute("UPDATE students SET ref=? WHERE id=?", (ref, student_id))
    db.commit()
    audit(actor or f"student:{ref}", f"{action} {ref}" if actor else action)
    return ref


# ---------- documents, schedule, fees, payments ----------
def docs_for(student_id):
    rows = {r["doc_type"]: r for r in get_db().execute("SELECT * FROM documents WHERE student_id=?", (student_id,))}
    out = []
    for d in DOCS:
        r = rows.get(d)
        out.append({"type": d, "id": r["id"] if r else None, "status": r["status"] if r else "missing",
                    "file": r["original"] if r else "", "note": r["note"] if r else ""})
    return out


def seats_taken(section_id, exclude_student=None):
    q = ("SELECT COUNT(*) n FROM enrollments e JOIN advising a ON a.student_id=e.student_id "
         "WHERE e.section_id=? AND a.status IN ('submitted','approved')")
    args = [section_id]
    if exclude_student:
        q += " AND e.student_id!=?"
        args.append(exclude_student)
    return get_db().execute(q, args).fetchone()["n"]


def schedule_rows(student_id):
    return get_db().execute(
        """SELECT sub.code, sub.title, sub.units, sec.id sec_id, sec.section_code, sec.days,
                  sec.time_start, sec.time_end, sec.room
           FROM enrollments e JOIN sections sec ON sec.id=e.section_id JOIN subjects sub ON sub.id=sec.subject_id
           WHERE e.student_id=? ORDER BY sub.code""", (student_id,)).fetchall()


def assessment(student_id):
    rows = schedule_rows(student_id)
    units = sum(r["units"] for r in rows)
    lines = []
    if rows:
        rate = float(setting("tuition_per_unit"))
        lines.append((f"Tuition ({units} units at {peso(rate)} per unit)", round(units * rate, 2)))
        for f in get_db().execute("SELECT * FROM fees ORDER BY id"):
            lines.append((f["name"], round(f["amount"], 2)))
    return {"rows": rows, "units": units, "lines": lines, "total": round(sum(a for _, a in lines), 2)}


def paid_total(student_id):
    r = get_db().execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE student_id=?", (student_id,)).fetchone()
    return round(float(r["s"]), 2)


def plan_for(student_id):
    return get_db().execute("SELECT * FROM payment_plans WHERE student_id=?", (student_id,)).fetchone()


def required_amount(total, plan):
    return round(total * 0.5, 2) if plan == "installment" else total


def progress(row):
    sid = row["id"]
    docs = docs_for(sid)
    states = [d["status"] for d in docs]
    docs_ok = all(s == "accepted" for s in states)
    docs_in = all(s != "missing" for s in states)
    adv = get_db().execute("SELECT status, note FROM advising WHERE student_id=?", (sid,)).fetchone()
    adv_status = adv["status"] if adv else "none"
    a = assessment(sid)
    paid = paid_total(sid)
    plan = plan_for(sid)
    required = required_amount(a["total"], plan["plan"] if plan else "full")
    pay_ok = bool(a["rows"]) and paid + 0.005 >= required
    enrolled = docs_ok and adv_status == "approved" and pay_ok
    if enrolled:
        label, css = "Officially enrolled", "ok"
    elif "rejected" in states:
        label, css = "Action needed: document", "bad"
    elif adv_status == "rejected":
        label, css = "Action needed: subjects", "bad"
    elif not docs_ok:
        label, css = ("Documents under review" if docs_in else "Documents incomplete"), "warn"
    elif adv_status in ("none", "draft"):
        label, css = "Needs subject advising", "warn"
    elif adv_status == "submitted":
        label, css = "Subjects awaiting approval", "warn"
    else:
        label, css = "Awaiting payment", "warn"
    steps = [("Registered", True), ("Documents accepted", docs_ok), ("Subjects approved", adv_status == "approved"),
             ("Payment received", pay_ok), ("Officially enrolled", enrolled)]
    return {"docs": docs, "label": label, "css": css, "steps": steps, "adv_status": adv_status,
            "adv_note": adv["note"] if adv else "", "assessment": a, "paid": paid, "plan": plan,
            "required": required, "balance": round(max(0, a["total"] - paid), 2), "enrolled": enrolled,
            "docs_ok": docs_ok, "docs_in": docs_in}


def summary(row):
    s = progress(row)
    s["row"] = row
    return s


# ---------- QR tokens ----------
def _sig(ref, exp):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{ref}|{exp}".encode(), hashlib.sha256).hexdigest()[:20]


def make_token(ref):
    exp = int(time.time()) + current_app.config["TOKEN_DAYS"] * 86400
    return f"{ref}.{exp}.{_sig(ref, exp)}"


def check_token(token):
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return False, "Not a BCC code.", None
    ref, exp, sig = parts[0], int(parts[1]), parts[2]
    if not hmac.compare_digest(sig, _sig(ref, exp)):
        return False, "Code was altered or forged.", None
    if exp < time.time():
        return False, "Code has expired. Ask the student for a new one.", None
    row = student_by_ref(ref)
    if not row:
        return False, "No such student.", None
    return True, "Valid code.", row


def verify_url(ref):
    base = (current_app.config.get("PUBLIC_URL") or request.host_url).rstrip("/")
    return f"{base}/v/{make_token(ref)}"


def extract_token(text):
    text = (text or "").strip()
    if "/v/" in text:
        text = text.split("/v/", 1)[1]
    return text.split("?", 1)[0].split("#", 1)[0].strip()


def rate_limited():
    hits = current_app.extensions.setdefault("scan_hits", {})
    key = request.remote_addr or "unknown"
    now_time = time.time()
    recent = [stamp for stamp in hits.get(key, []) if now_time - stamp < 60]
    recent.append(now_time)
    hits[key] = recent
    return len(recent) > current_app.config["RATE_LIMIT_PER_MIN"]


# ---------- access control ----------
def registrar_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("registrar"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return wrapper


def student_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        ref = session.get("ref")
        if not ref:
            return redirect(url_for("lookup"))
        if not student_by_ref(ref):
            session.pop("ref", None)
            flash("That student session is no longer valid. Check your status again.", "bad")
            return redirect(url_for("lookup"))
        return f(*a, **k)
    return wrapper


def current_student():
    return student_by_ref(session["ref"])


# ---------- app ----------
def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        REGISTRAR_PASSWORD=os.environ.get("REGISTRAR_PASSWORD", "registrar123"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024, TOKEN_DAYS=30, DEADLINE="2027-07-12",
        SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PUBLIC_URL=os.environ.get("PUBLIC_URL", ""),
        RATE_LIMIT_PER_MIN=60)
    # On read-only serverless filesystems (like Vercel / AWS Lambda), store in /tmp if instance_path is not writeable
    upload_dir = "/tmp/uploads" if os.environ.get("VERCEL") else os.path.join(app.instance_path, "uploads")
    db_path = "/tmp/uers.db" if os.environ.get("VERCEL") else os.path.join(app.instance_path, "uers.db")
    app.config["DATABASE"] = db_path
    app.config["UPLOAD_DIR"] = upload_dir
    if os.environ.get("DATABASE_URL"):
        app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL")
    if config:
        app.config.update(config)
    try:
        os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    except Exception:
        pass
    db = get_db_connection(app.config)
    if not db.is_pg:
        db.executescript(SCHEMA)
        if "year_level" not in [r[1] for r in db.execute("PRAGMA table_info(students)")]:
            db.execute("ALTER TABLE students ADD COLUMN year_level INTEGER NOT NULL DEFAULT 1")
    else:
        db.executescript(PG_SCHEMA)
    db.commit()
    db.close()
    app.add_template_filter(peso, "peso")

    @app.teardown_appcontext
    def close_db(_):
        db = g.pop("db", None)
        if db:
            db.close()

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf": session["csrf"], "is_registrar": bool(session.get("registrar")),
                "is_student": bool(session.get("ref")), "DOCS": DOCS, "deadline": app.config["DEADLINE"]}

    @app.before_request
    def csrf_check():
        if request.method == "POST":
            if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "none")):
                abort(400, "Missing or wrong form token.")

    @app.errorhandler(413)
    def too_big(_):
        return "File is larger than 2 MB.", 413

    # ----- student side: registration, status, documents, QR -----
    @app.get("/")
    def home():
        if session.get("registrar"):
            return redirect(url_for("dashboard"))
        return render_template("register.html", programs=PROGRAMS, campuses=CAMPUSES, form={})

    @app.post("/register")
    def register():
        if session.get("registrar"):
            return redirect(url_for("dashboard"))
        f = request.form
        errs, clean = validate_student(f)
        if errs:
            for e in errs:
                flash(e, "bad")
            return render_template("register.html", programs=PROGRAMS, campuses=CAMPUSES, form=f), 400
        session["ref"] = insert_student(clean)
        return redirect(url_for("student"))

    @app.route("/lookup", methods=["GET", "POST"])
    def lookup():
        if session.get("registrar"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            row = get_db().execute("SELECT * FROM students WHERE ref=? AND email=?", (
                request.form.get("ref", "").strip().upper(), request.form.get("email", "").strip().lower())).fetchone()
            if row:
                session["ref"] = row["ref"]
                return redirect(url_for("student"))
            flash("No student matches that reference number and email.", "bad")
        return render_template("lookup.html")

    @app.get("/student")
    @student_required
    def student():
        return render_template("student.html", s=summary(current_student()), term=(setting("school_year"), setting("semester")))

    @app.post("/student/upload")
    @student_required
    def upload():
        db = get_db()
        row = current_student()
        doc_type, file = request.form.get("doc_type"), request.files.get("file")
        if doc_type not in DOCS or not file or not file.filename:
            flash("Choose a requirement and a file.", "bad")
            return redirect(url_for("student"))
        ext = os.path.splitext(file.filename)[1].lower()
        head = file.stream.read(8)
        file.stream.seek(0)
        if ext not in MAGIC or not head.startswith(MAGIC[ext]):
            flash("Only real PDF, JPG or PNG files are accepted.", "bad")
            return redirect(url_for("student"))
        stored = uuid.uuid4().hex + ext
        file.save(os.path.join(current_app.config["UPLOAD_DIR"], stored))
        old = db.execute("SELECT stored FROM documents WHERE student_id=? AND doc_type=?", (row["id"], doc_type)).fetchone()
        db.execute("""INSERT INTO documents(student_id,doc_type,original,stored,status,note,updated)
                      VALUES(?,?,?,?, 'submitted','',?)
                      ON CONFLICT(student_id,doc_type) DO UPDATE SET original=excluded.original,
                      stored=excluded.stored,status='submitted',note='',updated=excluded.updated""",
                   (row["id"], doc_type, os.path.basename(file.filename)[:80], stored, now()))
        db.commit()
        if old and old["stored"]:
            try:
                os.remove(os.path.join(current_app.config["UPLOAD_DIR"], old["stored"]))
            except OSError:
                pass
        audit(f"student:{row['ref']}", f"uploaded {doc_type}")
        flash(f"{doc_type} submitted.", "ok")
        return redirect(url_for("student"))

    @app.get("/qr/<ref>.png")
    def qr(ref):
        if session.get("ref") != ref and not session.get("registrar"):
            abort(403)
        if not student_by_ref(ref):
            abort(404)
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        code.add_data(verify_url(ref))
        code.make(fit=True)
        buf = io.BytesIO()
        code.make_image().save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    @app.get("/v/<token>")
    def public_verify(token):
        if rate_limited():
            return render_template("scan.html", state="busy"), 429

        started = time.perf_counter()
        ok, message, row = check_token(token)
        elapsed = (time.perf_counter() - started) * 1000
        db = get_db()
        db.execute(
            "INSERT INTO verifications(ts,ref,ok,ms) VALUES(?,?,?,?)",
            (now(), row["ref"] if row else None, int(ok), elapsed),
        )
        db.commit()
        audit(
            "registrar scan" if session.get("registrar") else "public scan",
            f"QR opened for {row['ref']}" if ok else f"invalid QR opened ({message})",
        )

        if not ok:
            status = 410 if "expired" in message.lower() else 404
            return render_template("scan.html", state="invalid", message=message), status

        response = make_response(render_template(
            "scan.html",
            state="valid",
            message=message,
            s=summary(row),
            term=(setting("school_year"), setting("semester")),
        ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ----- registrar side: login, dashboard, documents, verification, audit -----
    @app.route("/registrar/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", ""), current_app.config["REGISTRAR_PASSWORD"]):
                session["registrar"] = True
                audit("registrar", "logged in")
                return redirect(request.args.get("next") or url_for("dashboard"))
            audit("unknown", "failed registrar login")
            flash("Wrong password.", "bad")
        return render_template("login.html")

    @app.get("/registrar")
    @registrar_required
    def dashboard():
        db = get_db()
        rows = [summary(r) for r in db.execute("SELECT * FROM students ORDER BY id")]
        total = len(rows)
        enrolled = sum(1 for s in rows if s["enrolled"])
        action = sum(1 for s in rows if s["label"].startswith("Action"))
        all_in = sum(1 for s in rows if s["docs_in"])
        v = db.execute("SELECT COUNT(*) n, AVG(ms) a FROM verifications WHERE ok=1").fetchone()
        stats = {"total": total, "enrolled": enrolled, "action": action, "progress": total - enrolled - action,
                 "all_in": all_in, "pct": round(100 * all_in / total) if total else 0,
                 "pending_adv": sum(1 for s in rows if s["adv_status"] == "submitted"),
                 "assessed": round(sum(s["assessment"]["total"] for s in rows), 2),
                 "collected": round(sum(s["paid"] for s in rows), 2),
                 "campus": db.execute("SELECT campus k, COUNT(*) n FROM students GROUP BY campus").fetchall(),
                 "program": db.execute("SELECT program k, COUNT(*) n FROM students GROUP BY program").fetchall(),
                 "checks": v["n"], "avg_ms": round(v["a"], 1) if v["a"] else None}
        return render_template("dashboard.html", st=stats, rows=rows)

    @app.get("/registrar/student/<ref>")
    @registrar_required
    def detail(ref):
        row = student_by_ref(ref)
        if not row:
            abort(404)
        pays = get_db().execute("SELECT * FROM payments WHERE student_id=? ORDER BY id", (row["id"],)).fetchall()
        return render_template("detail.html", s=summary(row), pays=pays, METHODS=METHODS, PLANS=PLANS)

    @app.post("/registrar/doc/<int:doc_id>/<action>")
    @registrar_required
    def review(doc_id, action):
        if action not in ("accept", "reject"):
            abort(404)
        db = get_db()
        d = db.execute("SELECT d.*, s.ref FROM documents d JOIN students s ON s.id=d.student_id WHERE d.id=?", (doc_id,)).fetchone()
        if not d:
            abort(404)
        note = request.form.get("note", "").strip()[:200] or ("Please upload a clearer copy." if action == "reject" else "")
        db.execute("UPDATE documents SET status=?, note=?, updated=? WHERE id=?",
                   ("accepted" if action == "accept" else "rejected", note if action == "reject" else "", now(), doc_id))
        db.commit()
        audit("registrar", f"{action}ed {d['doc_type']} for {d['ref']}")
        return redirect(url_for("detail", ref=d["ref"]))

    @app.get("/registrar/file/<int:doc_id>")
    @registrar_required
    def file(doc_id):
        d = get_db().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d:
            abort(404)
        audit("registrar", f"opened file {d['doc_type']} (doc {doc_id})")
        return send_file(os.path.join(current_app.config["UPLOAD_DIR"], d["stored"]),
                         download_name=d["original"], as_attachment=True)

    @app.route("/registrar/verify", methods=["GET", "POST"])
    @registrar_required
    def verify():
        result = None
        if request.method == "POST":
            t0 = time.perf_counter()
            ok, msg, row = check_token(extract_token(request.form.get("token", "")))
            ms = (time.perf_counter() - t0) * 1000
            ref = row["ref"] if row else None
            db = get_db()
            db.execute("INSERT INTO verifications(ts,ref,ok,ms) VALUES(?,?,?,?)", (now(), ref, int(ok), ms))
            db.commit()
            audit("registrar", f"verified code for {ref}" if ok else f"rejected a code ({msg})")
            result = {"ok": ok, "msg": msg, "ms": round(ms, 2), "s": summary(row) if row else None}
        return render_template("verify.html", result=result)

    @app.get("/registrar/audit")
    @registrar_required
    def audit_log():
        rows = get_db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()
        return render_template("audit.html", rows=rows)

    from academics import register_academics
    register_academics(app)
    return app


# Lazy top-level application wrapper for WSGI / Vercel
def application(environ, start_response):
    global _app
    if "_app" not in globals():
        _app = create_app()
    return _app(environ, start_response)

app = application

if __name__ == "__main__":
    create_app().run(debug=False)
