"""BCC-UERS: simple working enrollment system (registration, documents, QR verification, dashboard).

Flask + SQLite. Built as evidence for the Bacolod City College proposal.
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

import qrcode
from flask import (Flask, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

DOCS = ["Report card or TOR", "Valid ID", "Birth certificate", "2x2 photo"]
PROGRAMS = ["BEED", "BSED", "BSBA-MM", "BSEntrep", "BSOA", "BSIS"]
CAMPUSES = ["Taculing", "Sum-ag"]
MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL, program TEXT NOT NULL, campus TEXT NOT NULL,
  consent INTEGER NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id),
  doc_type TEXT NOT NULL, original TEXT, stored TEXT, status TEXT NOT NULL DEFAULT 'submitted',
  note TEXT DEFAULT '', updated TEXT NOT NULL, UNIQUE(student_id, doc_type));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ref TEXT, ok INTEGER NOT NULL, ms REAL NOT NULL);
"""


# ---------- helpers ----------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def audit(actor, action):
    get_db().execute("INSERT INTO audit(ts,actor,action) VALUES(?,?,?)", (now(), actor, action))
    get_db().commit()


def status_of(states):
    if "rejected" in states:
        return "Action needed", "bad"
    if all(s == "accepted" for s in states):
        return "Enrolled", "ok"
    if all(s != "missing" for s in states):
        return "Under review", "warn"
    return "Documents incomplete", "warn"


def docs_for(student_id):
    rows = {r["doc_type"]: r for r in get_db().execute(
        "SELECT * FROM documents WHERE student_id=?", (student_id,))}
    out = []
    for d in DOCS:
        r = rows.get(d)
        out.append({"type": d, "id": r["id"] if r else None, "status": r["status"] if r else "missing",
                    "file": r["original"] if r else "", "note": r["note"] if r else ""})
    return out


def summary(row):
    docs = docs_for(row["id"])
    label, css = status_of([d["status"] for d in docs])
    return {"row": row, "docs": docs, "label": label, "css": css}


def make_token(ref):
    exp = int(time.time()) + current_app.config["TOKEN_DAYS"] * 86400
    return f"{ref}.{exp}.{_sig(ref, exp)}"


def _sig(ref, exp):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{ref}|{exp}".encode(), hashlib.sha256).hexdigest()[:20]


def check_token(token):
    """Returns (ok, message, student_row)."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return False, "Not a BCC code.", None
    ref, exp, sig = parts[0], int(parts[1]), parts[2]
    if not hmac.compare_digest(sig, _sig(ref, exp)):
        return False, "Code was altered or forged.", None
    if exp < time.time():
        return False, "Code has expired. Ask the student for a new one.", None
    row = get_db().execute("SELECT * FROM students WHERE ref=?", (ref,)).fetchone()
    if not row:
        return False, "No such student.", None
    return True, "Valid code.", row


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
        if not session.get("ref"):
            return redirect(url_for("lookup"))
        return f(*a, **k)
    return wrapper


# ---------- app ----------
def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        REGISTRAR_PASSWORD=os.environ.get("REGISTRAR_PASSWORD", "registrar123"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024, TOKEN_DAYS=30, DEADLINE="2027-07-12",
        SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1")
    app.config["DATABASE"] = os.path.join(app.instance_path, "uers.db")
    app.config["UPLOAD_DIR"] = os.path.join(app.instance_path, "uploads")
    if config:
        app.config.update(config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as db:
        db.executescript(SCHEMA)

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
                "is_student": bool(session.get("ref")),
                "DOCS": DOCS, "deadline": app.config["DEADLINE"]}

    @app.before_request
    def csrf_check():
        if request.method == "POST":
            if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "none")):
                abort(400, "Missing or wrong form token.")

    @app.errorhandler(413)
    def too_big(_):
        return "File is larger than 2 MB.", 413

    # ----- student side -----
    @app.get("/")
    def home():
        return render_template("register.html", programs=PROGRAMS, campuses=CAMPUSES, form={})

    @app.post("/register")
    def register():
        f = request.form
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        errs = []
        if not 2 <= len(name) <= 80:
            errs.append("Enter your full name (2 to 80 characters).")
        if not EMAIL.match(email) or len(email) > 80:
            errs.append("Enter a valid email address.")
        if f.get("program") not in PROGRAMS or f.get("campus") not in CAMPUSES:
            errs.append("Choose a program and a campus.")
        if f.get("consent") != "on":
            errs.append("You must agree to the data privacy notice to register.")
        db = get_db()
        if not errs and db.execute("SELECT 1 FROM students WHERE email=?", (email,)).fetchone():
            errs.append("This email is already registered. Use 'Check my status'.")
        if errs:
            for e in errs:
                flash(e, "bad")
            return render_template("register.html", programs=PROGRAMS, campuses=CAMPUSES, form=f), 400
        cur = db.execute("INSERT INTO students(name,email,program,campus,consent,created) VALUES(?,?,?,?,1,?)",
                         (name, email, f["program"], f["campus"], now()))
        ref = f"BCC-2027-{cur.lastrowid:04d}"
        db.execute("UPDATE students SET ref=? WHERE id=?", (ref, cur.lastrowid))
        db.commit()
        audit(f"student:{ref}", "registered")
        session["ref"] = ref
        return redirect(url_for("student"))

    @app.route("/lookup", methods=["GET", "POST"])
    def lookup():
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
        row = get_db().execute("SELECT * FROM students WHERE ref=?", (session["ref"],)).fetchone()
        return render_template("student.html", s=summary(row))

    @app.post("/student/upload")
    @student_required
    def upload():
        db = get_db()
        row = db.execute("SELECT * FROM students WHERE ref=?", (session["ref"],)).fetchone()
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
        if not get_db().execute("SELECT 1 FROM students WHERE ref=?", (ref,)).fetchone():
            abort(404)
        buf = io.BytesIO()
        qrcode.make(make_token(ref)).save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ----- registrar side -----
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
        by = {"Enrolled": 0, "Under review": 0, "Action needed": 0, "Documents incomplete": 0}
        for s in rows:
            by[s["label"]] += 1
        all_in = sum(1 for s in rows if all(d["status"] != "missing" for d in s["docs"]))
        v = db.execute("SELECT COUNT(*) n, AVG(ms) a FROM verifications WHERE ok=1").fetchone()
        stats = {"total": total, "by": by, "all_in": all_in, "pct": round(100 * all_in / total) if total else 0,
                 "campus": db.execute("SELECT campus k, COUNT(*) n FROM students GROUP BY campus").fetchall(),
                 "program": db.execute("SELECT program k, COUNT(*) n FROM students GROUP BY program").fetchall(),
                 "checks": v["n"], "avg_ms": round(v["a"], 1) if v["a"] else None}
        return render_template("dashboard.html", st=stats, rows=rows)

    @app.get("/registrar/student/<ref>")
    @registrar_required
    def detail(ref):
        row = get_db().execute("SELECT * FROM students WHERE ref=?", (ref,)).fetchone()
        if not row:
            abort(404)
        return render_template("detail.html", s=summary(row))

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
            ok, msg, row = check_token(request.form.get("token", ""))
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

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
