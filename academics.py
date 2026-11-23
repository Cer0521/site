"""Academic side of BCC-UERS: curriculum, class sections, advising, fee assessment,
payment choice, student records and reports. These cover the process the 2009 study digitized.
"""
import csv
import io
import re
import sqlite3

try:
    import psycopg2
    PG_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
except ImportError:
    PG_ERRORS = (sqlite3.IntegrityError,)

from flask import Response, abort, flash, redirect, render_template, request, url_for

from app import (METHODS, PLANS, PROGRAMS, CAMPUSES, audit, current_student, get_db, insert_student,
                 make_token, now, peso, required_amount, registrar_required, seats_taken, set_setting,
                 setting, student_by_ref, student_required, summary, validate_student)

DAY_RE = re.compile(r"Th|M|T|W|F|S")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

SAMPLE_SUBJECTS = [  # (code, title, units, program, year, semester)
    ("GE 101", "Understanding the Self", 3, "ALL", 1, "1"), ("GE 102", "Purposive Communication", 3, "ALL", 1, "1"),
    ("GE 103", "Mathematics in the Modern World", 3, "ALL", 1, "1"), ("PE 101", "Physical Fitness", 2, "ALL", 1, "1"),
    ("NSTP 101", "National Service Training Program 1", 3, "ALL", 1, "1"),
    ("IS 101", "Introduction to Information Systems", 3, "BSIS", 1, "1"), ("IT 101", "Computer Programming 1", 3, "BSIS", 1, "1"),
    ("ED 101", "Child and Adolescent Development", 3, "BEED", 1, "1"), ("ED 102", "The Teaching Profession", 3, "BSED", 1, "1"),
    ("BA 101", "Principles of Marketing", 3, "BSBA-MM", 1, "1"), ("EN 101", "The Entrepreneurial Mind", 3, "BSEntrep", 1, "1"),
    ("OA 101", "Keyboarding and Document Processing", 3, "BSOA", 1, "1"),
]
SAMPLE_SECTIONS = {  # code -> [(section, days, start, end, room, capacity)]
    "GE 101": [("A", "MWF", "08:00", "09:00", "Room 101", 40), ("B", "TTh", "08:00", "09:30", "Room 102", 40)],
    "GE 102": [("A", "MWF", "09:00", "10:00", "Room 101", 40), ("B", "TTh", "09:30", "11:00", "Room 102", 40)],
    "GE 103": [("A", "MWF", "10:00", "11:00", "Room 101", 40), ("B", "TTh", "11:00", "12:30", "Room 102", 40)],
    "PE 101": [("A", "T", "13:00", "15:00", "Gym", 50), ("B", "Th", "13:00", "15:00", "Gym", 50)],
    "NSTP 101": [("A", "S", "08:00", "11:00", "Room 201", 60), ("B", "S", "13:00", "16:00", "Room 201", 60)],
    "IS 101": [("A", "MWF", "13:00", "14:00", "IS Lab 1", 40)], "IT 101": [("A", "TTh", "15:00", "16:30", "IS Lab 1", 40)],
    "ED 101": [("A", "MWF", "13:00", "14:00", "Room 301", 40)], "ED 102": [("A", "MWF", "13:00", "14:00", "Room 302", 40)],
    "BA 101": [("A", "MWF", "13:00", "14:00", "Room 303", 40)], "EN 101": [("A", "MWF", "13:00", "14:00", "Room 304", 40)],
    "OA 101": [("A", "MWF", "13:00", "14:00", "Room 305", 40)],
}
SAMPLE_FEES = [("Sample registration fee", 150.0), ("Sample library fee", 100.0)]


def day_set(d):
    return set(DAY_RE.findall(d))


def valid_days(d):
    return bool(d) and "".join(DAY_RE.findall(d)) == d


def minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def overlaps(a, b):
    return (bool(day_set(a["days"]) & day_set(b["days"])) and minutes(a["time_start"]) < minutes(b["time_end"])
            and minutes(b["time_start"]) < minutes(a["time_end"]))


def safe_cell(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([safe_cell(x) for x in r])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


def section_row(section_id):
    return get_db().execute(
        """SELECT sec.*, sub.code, sub.title, sub.units, sub.program, sub.year_level, sub.semester
           FROM sections sec JOIN subjects sub ON sub.id=sec.subject_id WHERE sec.id=?""", (section_id,)).fetchone()


def seed_demo():
    db = get_db()
    for k, v in {"school_year": "2027-2028", "semester": "1", "tuition_per_unit": "30", "max_units": "24",
                 "sample_data": "1"}.items():
        set_setting(k, v)
    for s in SAMPLE_SUBJECTS:
        db.execute("INSERT OR IGNORE INTO subjects(code,title,units,program,year_level,semester) VALUES(?,?,?,?,?,?)", s)
    for code, secs in SAMPLE_SECTIONS.items():
        sid = db.execute("SELECT id FROM subjects WHERE code=?", (code,)).fetchone()["id"]
        for sec in secs:
            db.execute("""INSERT OR IGNORE INTO sections(subject_id,section_code,days,time_start,time_end,room,capacity)
                          VALUES(?,?,?,?,?,?,?)""", (sid, *sec))
    if not db.execute("SELECT 1 FROM fees").fetchone():
        for f in SAMPLE_FEES:
            db.execute("INSERT INTO fees(name,amount) VALUES(?,?)", f)
    db.commit()
    audit("system", "loaded sample curriculum, sections and fees")


def register_academics(app):
    @app.cli.command("seed-demo")
    def seed_cmd():
        """Load a sample curriculum, class sections and fees."""
        seed_demo()
        print("Sample curriculum, sections and fees loaded.")

    # ------------------------------------------------------------------ curriculum
    @app.route("/registrar/subjects", methods=["GET", "POST"])
    @registrar_required
    def subjects():
        db = get_db()
        if request.method == "POST":
            f = request.form
            code, title = f.get("code", "").strip().upper(), f.get("title", "").strip()
            try:
                units, year = int(f.get("units", "")), int(f.get("year_level", ""))
            except ValueError:
                units = year = 0
            program, sem = f.get("program"), f.get("semester")
            errs = []
            if not re.fullmatch(r"[A-Z0-9 ]{2,12}", code):
                errs.append("Subject code: 2 to 12 letters or numbers.")
            if not 2 <= len(title) <= 80:
                errs.append("Subject title: 2 to 80 characters.")
            if not 1 <= units <= 6:
                errs.append("Units must be from 1 to 6.")
            if program not in PROGRAMS + ["ALL"]:
                errs.append("Choose a program or ALL.")
            if not 1 <= year <= 4:
                errs.append("Year level must be 1 to 4.")
            if sem not in ("1", "2"):
                errs.append("Semester must be 1 or 2.")
            if not errs and db.execute("SELECT 1 FROM subjects WHERE code=?", (code,)).fetchone():
                errs.append("That subject code already exists.")
            if errs:
                for e in errs:
                    flash(e, "bad")
            else:
                db.execute("INSERT INTO subjects(code,title,units,program,year_level,semester) VALUES(?,?,?,?,?,?)",
                           (code, title, units, program, year, sem))
                db.commit()
                audit("registrar", f"added subject {code}")
                flash(f"Subject {code} added.", "ok")
            return redirect(url_for("subjects"))
        rows = db.execute("SELECT * FROM subjects ORDER BY year_level, semester, code").fetchall()
        return render_template("subjects.html", rows=rows, programs=PROGRAMS + ["ALL"])

    @app.post("/registrar/subjects/import")
    @registrar_required
    def subjects_import():
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Choose a .csv file.", "bad")
            return redirect(url_for("subjects"))
        reader = csv.DictReader(io.StringIO(file.stream.read().decode("utf-8-sig", errors="replace")))
        need = {"code", "title", "units", "program", "year_level", "semester"}
        if not reader.fieldnames or not need <= {h.strip() for h in reader.fieldnames}:
            flash("The CSV needs these columns: code, title, units, program, year_level, semester.", "bad")
            return redirect(url_for("subjects"))
        db = get_db()
        added = skipped = 0
        for i, r in enumerate(reader):
            if i >= 500:
                break
            try:
                code, title = r["code"].strip().upper(), r["title"].strip()
                units, year, program, sem = int(r["units"]), int(r["year_level"]), r["program"].strip(), r["semester"].strip()
                ok = (re.fullmatch(r"[A-Z0-9 ]{2,12}", code) and 2 <= len(title) <= 80 and 1 <= units <= 6
                      and program in PROGRAMS + ["ALL"] and 1 <= year <= 4 and sem in ("1", "2"))
                if not ok:
                    raise ValueError
                db.execute("INSERT INTO subjects(code,title,units,program,year_level,semester) VALUES(?,?,?,?,?,?)",
                           (code, title, units, program, year, sem))
                added += 1
            except (ValueError, KeyError, AttributeError, *PG_ERRORS):
                skipped += 1
        db.commit()
        audit("registrar", f"imported subjects: {added} added, {skipped} skipped")
        flash(f"Imported {added} subjects; skipped {skipped} invalid or duplicate rows.", "ok" if added else "bad")
        return redirect(url_for("subjects"))

    # ------------------------------------------------------------------ class sections
    @app.route("/registrar/sections", methods=["GET", "POST"])
    @registrar_required
    def sections():
        db = get_db()
        if request.method == "POST":
            f = request.form
            errs = []
            try:
                subject_id, cap = int(f.get("subject_id", "")), int(f.get("capacity", ""))
            except ValueError:
                subject_id = cap = 0
            code, days = f.get("section_code", "").strip().upper(), f.get("days", "").strip()
            t1, t2, room = f.get("time_start", ""), f.get("time_end", ""), f.get("room", "").strip()
            if not db.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
                errs.append("Choose a subject.")
            if not re.fullmatch(r"[A-Z0-9]{1,6}", code):
                errs.append("Section code: 1 to 6 letters or numbers.")
            if not valid_days(days):
                errs.append("Days must use M, T, W, Th, F, S, for example MWF or TTh.")
            if not (TIME_RE.match(t1) and TIME_RE.match(t2)) or (TIME_RE.match(t1) and TIME_RE.match(t2) and minutes(t1) >= minutes(t2)):
                errs.append("Enter start and end times as HH:MM, with the start before the end.")
            if not 2 <= len(room) <= 30:
                errs.append("Enter a room (2 to 30 characters).")
            if not 1 <= cap <= 200:
                errs.append("Capacity must be 1 to 200.")
            if not errs:
                new = {"days": days, "time_start": t1, "time_end": t2}
                for o in db.execute("SELECT sec.*, sub.code subject FROM sections sec JOIN subjects sub ON sub.id=sec.subject_id WHERE lower(sec.room)=lower(?)", (room,)):
                    if overlaps(new, o):
                        errs.append(f"Room {room} is already used by {o['subject']} section {o['section_code']} at that time.")
                        break
            if errs:
                for e in errs:
                    flash(e, "bad")
            else:
                try:
                    db.execute("""INSERT INTO sections(subject_id,section_code,days,time_start,time_end,room,capacity)
                                  VALUES(?,?,?,?,?,?,?)""", (subject_id, code, days, t1, t2, room, cap))
                    db.commit()
                    audit("registrar", f"added section {code} for subject id {subject_id}")
                    flash("Section added.", "ok")
                except PG_ERRORS:
                    flash("That subject already has a section with this code.", "bad")
            return redirect(url_for("sections"))
        rows = db.execute("""SELECT sec.*, sub.code, sub.title, sub.units FROM sections sec
                             JOIN subjects sub ON sub.id=sec.subject_id ORDER BY sub.code, sec.section_code""").fetchall()
        subs = db.execute("SELECT id, code, title FROM subjects ORDER BY code").fetchall()
        return render_template("sections.html", rows=[{"r": r, "seats": seats_taken(r["id"])} for r in rows], subs=subs)

    @app.post("/registrar/section/<int:sid>/delete")
    @registrar_required
    def section_delete(sid):
        db = get_db()
        if db.execute("SELECT 1 FROM enrollments WHERE section_id=?", (sid,)).fetchone():
            flash("This section has students enrolled and cannot be deleted.", "bad")
        else:
            db.execute("DELETE FROM sections WHERE id=?", (sid,))
            db.commit()
            audit("registrar", f"deleted section id {sid}")
            flash("Section deleted.", "ok")
        return redirect(url_for("sections"))

    @app.get("/registrar/section/<int:sid>")
    @registrar_required
    def section_detail(sid):
        sec = section_row(sid)
        if not sec:
            abort(404)
        roster = get_db().execute(
            """SELECT s.ref, s.name, s.program, s.year_level, a.status FROM enrollments e
               JOIN students s ON s.id=e.student_id JOIN advising a ON a.student_id=s.id
               WHERE e.section_id=? ORDER BY s.name""", (sid,)).fetchall()
        return render_template("section_detail.html", sec=sec, roster=roster, seats=seats_taken(sid))

    # ------------------------------------------------------------------ term, tuition and fees
    @app.get("/registrar/fees")
    @registrar_required
    def fees():
        return render_template("fees.html", fees=get_db().execute("SELECT * FROM fees ORDER BY id").fetchall(),
                               cfg={k: setting(k) for k in ("school_year", "semester", "tuition_per_unit", "max_units", "sample_data")})

    @app.post("/registrar/settings")
    @registrar_required
    def settings_save():
        f = request.form
        errs = []
        try:
            rate, mx = float(f.get("tuition_per_unit", "")), int(f.get("max_units", ""))
        except ValueError:
            rate, mx = -1, 0
        if not re.fullmatch(r"\d{4}-\d{4}", f.get("school_year", "")):
            errs.append("School year looks like 2027-2028.")
        if f.get("semester") not in ("1", "2"):
            errs.append("Semester must be 1 or 2.")
        if not 0 <= rate <= 10000:
            errs.append("Tuition per unit must be from 0 to 10000.")
        if not 1 <= mx <= 30:
            errs.append("Maximum units must be 1 to 30.")
        if errs:
            for e in errs:
                flash(e, "bad")
        else:
            for k, v in (("school_year", f["school_year"]), ("semester", f["semester"]), ("tuition_per_unit", rate),
                         ("max_units", mx), ("sample_data", "1" if f.get("sample_data") == "on" else "0")):
                set_setting(k, v)
            audit("registrar", "updated term and tuition settings")
            flash("Settings saved.", "ok")
        return redirect(url_for("fees"))

    @app.post("/registrar/fees/add")
    @registrar_required
    def fee_add():
        name = request.form.get("name", "").strip()
        try:
            amount = round(float(request.form.get("amount", "")), 2)
        except ValueError:
            amount = -1
        if not 2 <= len(name) <= 60 or not 0 <= amount <= 100000:
            flash("Enter a fee name and an amount from 0 to 100000.", "bad")
        else:
            get_db().execute("INSERT INTO fees(name,amount) VALUES(?,?)", (name, amount))
            get_db().commit()
            audit("registrar", f"added fee {name}")
            flash("Fee added.", "ok")
        return redirect(url_for("fees"))

    @app.post("/registrar/fees/<int:fid>/delete")
    @registrar_required
    def fee_delete(fid):
        get_db().execute("DELETE FROM fees WHERE id=?", (fid,))
        get_db().commit()
        audit("registrar", f"deleted fee id {fid}")
        return redirect(url_for("fees"))

    # ------------------------------------------------------------------ student: advising
    def advising_groups(stu):
        db = get_db()
        chosen = {r["sec_id"] for r in db.execute("SELECT section_id sec_id FROM enrollments WHERE student_id=?", (stu["id"],))}
        groups = []
        for sub in db.execute("""SELECT * FROM subjects WHERE (program=? OR program='ALL') AND year_level=? AND semester=?
                                 ORDER BY code""", (stu["program"], stu["year_level"], setting("semester"))):
            secs = []
            for sec in db.execute("SELECT * FROM sections WHERE subject_id=? ORDER BY section_code", (sub["id"],)):
                taken = seats_taken(sec["id"], exclude_student=stu["id"])
                secs.append({"r": sec, "free": sec["capacity"] - taken, "chosen": sec["id"] in chosen})
            groups.append({"sub": sub, "secs": secs})
        return groups

    @app.get("/student/advising")
    @student_required
    def advising():
        stu = current_student()
        return render_template("advising.html", s=summary(stu), groups=advising_groups(stu),
                               max_units=setting("max_units"), sample=setting("sample_data") == "1",
                               term=(setting("school_year"), setting("semester")))

    @app.post("/student/advising")
    @student_required
    def advising_submit():
        stu, db = current_student(), get_db()
        cur = db.execute("SELECT status FROM advising WHERE student_id=?", (stu["id"],)).fetchone()
        if cur and cur["status"] == "approved":
            flash("Your subjects are approved. Ask the registrar to reopen them if you need to change.", "bad")
            return redirect(url_for("advising"))
        picks = {}
        try:
            for k, v in request.form.items():
                if k.startswith("sec_") and v:
                    picks[int(k[4:])] = int(v)
        except ValueError:
            abort(400)
        errs, chosen = [], []
        if not picks:
            errs.append("Choose at least one subject section.")
        for subject_id, section_id in picks.items():
            sec = section_row(section_id)
            if (not sec or sec["subject_id"] != subject_id or sec["semester"] != setting("semester")
                    or sec["year_level"] != stu["year_level"] or sec["program"] not in (stu["program"], "ALL")):
                errs.append("One of the chosen sections is not open to your program and year.")
                continue
            if sec["capacity"] - seats_taken(section_id, exclude_student=stu["id"]) <= 0:
                errs.append(f"{sec['code']} section {sec['section_code']} is full.")
            chosen.append(sec)
        units = sum(s["units"] for s in chosen)
        if units > int(setting("max_units")):
            errs.append(f"You chose {units} units; the limit is {setting('max_units')}.")
        for i, a in enumerate(chosen):
            for b in chosen[i + 1:]:
                if overlaps(a, b):
                    errs.append(f"{a['code']} and {b['code']} meet at the same time.")
        if errs:
            for e in dict.fromkeys(errs):
                flash(e, "bad")
            return redirect(url_for("advising"))
        db.execute("DELETE FROM enrollments WHERE student_id=?", (stu["id"],))
        for sec in chosen:
            db.execute("INSERT INTO enrollments(student_id,section_id) VALUES(?,?)", (stu["id"], sec["id"]))
        db.execute("""INSERT INTO advising(student_id,status,note,updated) VALUES(?, 'submitted','',?)
                      ON CONFLICT(student_id) DO UPDATE SET status='submitted', note='', updated=excluded.updated""",
                   (stu["id"], now()))
        db.commit()
        audit(f"student:{stu['ref']}", f"submitted {len(chosen)} subjects ({units} units) for approval")
        flash(f"Subjects submitted: {units} units. The registrar will review them.", "ok")
        return redirect(url_for("advising"))

    # ------------------------------------------------------------------ student: payment choice
    @app.route("/student/payment", methods=["GET", "POST"])
    @student_required
    def payment():
        stu = current_student()
        s = summary(stu)
        if not s["assessment"]["rows"]:
            flash("Choose your subjects first so your fees can be computed.", "bad")
            return redirect(url_for("advising"))
        if request.method == "POST":
            plan, method = request.form.get("plan"), request.form.get("method")
            if plan not in PLANS or method not in METHODS:
                flash("Choose a payment plan and a method.", "bad")
            else:
                get_db().execute("""INSERT INTO payment_plans(student_id,plan,method,chosen) VALUES(?,?,?,?)
                                    ON CONFLICT(student_id) DO UPDATE SET plan=excluded.plan, method=excluded.method,
                                    chosen=excluded.chosen""", (stu["id"], plan, method, now()))
                get_db().commit()
                audit(f"student:{stu['ref']}", f"chose {PLANS[plan]} via {METHODS[method]}")
                flash("Payment choice saved. Pay as chosen, then the registrar records your payment.", "ok")
            return redirect(url_for("payment"))
        pays = get_db().execute("SELECT * FROM payments WHERE student_id=? ORDER BY id", (stu["id"],)).fetchall()
        return render_template("payment.html", s=s, pays=pays, PLANS=PLANS, METHODS=METHODS)

    # ------------------------------------------------------------------ slips and certificate
    def slip(kind, row, back):
        s = summary(row)
        if kind in ("advising", "assessment") and not s["assessment"]["rows"]:
            flash("No subjects chosen yet.", "bad")
            return redirect(back)
        if kind == "certificate" and not s["enrolled"]:
            flash("The certificate is available only when the student is officially enrolled.", "bad")
            return redirect(back)
        return render_template(f"slip_{kind}.html", s=s, term=(setting("school_year"), setting("semester")),
                               token=make_token(row["ref"]) if kind == "certificate" else "", PLANS=PLANS, METHODS=METHODS)

    @app.get("/student/slip/<kind>")
    @student_required
    def student_slip(kind):
        if kind not in ("advising", "assessment", "certificate"):
            abort(404)
        return slip(kind, current_student(), url_for("student"))

    @app.get("/registrar/student/<ref>/slip/<kind>")
    @registrar_required
    def registrar_slip(ref, kind):
        row = student_by_ref(ref)
        if not row or kind not in ("advising", "assessment", "certificate"):
            abort(404)
        return slip(kind, row, url_for("detail", ref=ref))

    # ------------------------------------------------------------------ registrar: decisions and payments
    @app.post("/registrar/student/<ref>/advising/<action>")
    @registrar_required
    def advising_decision(ref, action):
        row = student_by_ref(ref)
        if not row or action not in ("approve", "reject"):
            abort(404)
        db = get_db()
        cur = db.execute("SELECT status FROM advising WHERE student_id=?", (row["id"],)).fetchone()
        status = cur["status"] if cur else "none"
        if action == "approve":
            if status != "submitted":
                flash("Only submitted subjects can be approved.", "bad")
                return redirect(url_for("detail", ref=ref))
            db.execute("UPDATE advising SET status='approved', note='', updated=? WHERE student_id=?", (now(), row["id"]))
        else:
            if status not in ("submitted", "approved"):
                flash("There are no submitted subjects to send back.", "bad")
                return redirect(url_for("detail", ref=ref))
            note = request.form.get("note", "").strip()[:200] or "Please revise your subjects."
            db.execute("UPDATE advising SET status='rejected', note=?, updated=? WHERE student_id=?", (note, now(), row["id"]))
        db.commit()
        audit("registrar", f"{action}d subjects for {ref}")
        return redirect(url_for("detail", ref=ref))

    @app.post("/registrar/student/<ref>/payment")
    @registrar_required
    def record_payment(ref):
        row = student_by_ref(ref)
        if not row:
            abort(404)
        s = summary(row)
        method, note = request.form.get("method"), request.form.get("note", "").strip()[:100]
        try:
            amount = round(float(request.form.get("amount", "")), 2)
        except ValueError:
            amount = 0
        if not s["assessment"]["rows"]:
            flash("No assessment yet, so no payment can be recorded.", "bad")
        elif method not in METHODS or amount <= 0:
            flash("Enter an amount above zero and choose a method.", "bad")
        elif amount > s["balance"] + 0.005:
            flash(f"That is more than the balance of {peso(s['balance'])}.", "bad")
        else:
            get_db().execute("INSERT INTO payments(student_id,amount,method,note,ts) VALUES(?,?,?,?,?)",
                             (row["id"], amount, method, note, now()))
            get_db().commit()
            audit("registrar", f"recorded payment {peso(amount)} for {ref}")
            flash(f"Payment of {peso(amount)} recorded.", "ok")
        return redirect(url_for("detail", ref=ref))

    # ------------------------------------------------------------------ student records
    @app.route("/registrar/student/<ref>/edit", methods=["GET", "POST"])
    @registrar_required
    def edit_student(ref):
        row = student_by_ref(ref)
        if not row:
            abort(404)
        if request.method == "POST":
            errs, c = validate_student(request.form, need_consent=False, check_email=False)
            if errs:
                for e in errs:
                    flash(e, "bad")
            else:
                db = get_db()
                changed = [k for k in ("name", "program", "campus", "year_level") if c[k] != row[k]]
                db.execute("UPDATE students SET name=?, program=?, campus=?, year_level=? WHERE id=?",
                           (c["name"], c["program"], c["campus"], c["year_level"], row["id"]))
                if "program" in changed or "year_level" in changed:
                    db.execute("DELETE FROM enrollments WHERE student_id=?", (row["id"],))
                    db.execute("DELETE FROM advising WHERE student_id=?", (row["id"],))
                    flash("Program or year changed, so the chosen subjects were cleared.", "ok")
                db.commit()
                audit("registrar", f"edited record {ref}: {', '.join(changed) or 'no changes'}")
                return redirect(url_for("detail", ref=ref))
        return render_template("edit_student.html", row=row, programs=PROGRAMS, campuses=CAMPUSES)

    @app.route("/registrar/students/new", methods=["GET", "POST"])
    @registrar_required
    def new_student():
        if request.method == "POST":
            errs, c = validate_student(request.form)
            if errs:
                for e in errs:
                    flash(e, "bad")
                return render_template("new_student.html", programs=PROGRAMS, campuses=CAMPUSES, form=request.form), 400
            ref = insert_student(c, actor="registrar", action="added walk-in record")
            flash(f"Record created: {ref}. The student can sign in with this reference number and their email.", "ok")
            return redirect(url_for("detail", ref=ref))
        return render_template("new_student.html", programs=PROGRAMS, campuses=CAMPUSES, form={})

    # ------------------------------------------------------------------ reports and exports
    @app.get("/registrar/reports")
    @registrar_required
    def reports():
        db = get_db()
        rows = [summary(r) for r in db.execute("SELECT * FROM students ORDER BY id")]
        groups = {}
        for s in rows:
            g = groups.setdefault((s["row"]["program"], s["row"]["year_level"]), {"total": 0, "enrolled": 0, "action": 0})
            g["total"] += 1
            g["enrolled"] += 1 if s["enrolled"] else 0
            g["action"] += 1 if s["label"].startswith("Action") else 0
        secs = db.execute("""SELECT sec.*, sub.code, sub.title FROM sections sec JOIN subjects sub ON sub.id=sec.subject_id
                             ORDER BY sub.code, sec.section_code""").fetchall()
        assessed = round(sum(s["assessment"]["total"] for s in rows), 2)
        collected = round(sum(s["paid"] for s in rows), 2)
        return render_template("reports.html", groups=sorted(groups.items()),
                               secs=[{"r": r, "seats": seats_taken(r["id"])} for r in secs],
                               assessed=assessed, collected=collected, outstanding=round(assessed - collected, 2),
                               total=len(rows))

    @app.get("/registrar/export/<name>.csv")
    @registrar_required
    def export(name):
        db = get_db()
        if name == "students":
            rows = []
            for r in db.execute("SELECT * FROM students ORDER BY id"):
                s = summary(r)
                rows.append([r["ref"], r["name"], r["email"], r["program"], r["campus"], r["year_level"], s["label"],
                             s["adv_status"], s["assessment"]["total"], s["paid"], s["balance"]])
            out = csv_response("students", ["ref", "name", "email", "program", "campus", "year_level", "stage",
                                            "subjects_status", "assessed", "paid", "balance"], rows)
        elif name == "enrollments":
            rows = db.execute(
                """SELECT s.ref, s.name, sub.code, sub.title, sub.units, sec.section_code, sec.days, sec.time_start,
                          sec.time_end, sec.room, a.status FROM enrollments e JOIN students s ON s.id=e.student_id
                   JOIN sections sec ON sec.id=e.section_id JOIN subjects sub ON sub.id=sec.subject_id
                   JOIN advising a ON a.student_id=s.id ORDER BY s.ref, sub.code""").fetchall()
            out = csv_response("enrollments", ["ref", "name", "subject", "title", "units", "section", "days", "start",
                                               "end", "room", "subjects_status"], [list(r) for r in rows])
        elif name == "payments":
            rows = db.execute("""SELECT p.ts, s.ref, s.name, p.amount, p.method, p.note FROM payments p
                                 JOIN students s ON s.id=p.student_id ORDER BY p.id""").fetchall()
            out = csv_response("payments", ["time", "ref", "name", "amount", "method", "note"], [list(r) for r in rows])
        else:
            abort(404)
        audit("registrar", f"exported {name}.csv")
        return out

    @app.get("/registrar/export/roster/<int:sid>.csv")
    @registrar_required
    def export_roster(sid):
        sec = section_row(sid)
        if not sec:
            abort(404)
        rows = get_db().execute(
            """SELECT s.ref, s.name, s.program, s.year_level, a.status FROM enrollments e
               JOIN students s ON s.id=e.student_id JOIN advising a ON a.student_id=s.id
               WHERE e.section_id=? ORDER BY s.name""", (sid,)).fetchall()
        audit("registrar", f"exported roster for section id {sid}")
        return csv_response(f"roster_{sec['code'].replace(' ', '')}_{sec['section_code']}",
                            ["ref", "name", "program", "year_level", "subjects_status"], [list(r) for r in rows])
