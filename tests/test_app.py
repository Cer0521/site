import io
import sqlite3
import time

import pytest

from academics import seed_demo
from app import DOCS, _sig, create_app, get_db, make_token

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


@pytest.fixture()
def app(tmp_path):
    a = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "UPLOAD_DIR": str(tmp_path / "up"),
                    "SECRET_KEY": "test-key", "REGISTRAR_PASSWORD": "pw"})
    with a.app_context():
        seed_demo()
    return a


@pytest.fixture()
def client(app):
    return app.test_client()


def post(client, url, data=None, **kw):
    with client.session_transaction() as s:
        s.setdefault("csrf", "tok")
        tok = s["csrf"]
    d = dict(data or {})
    d["csrf"] = tok
    return client.post(url, data=d, **kw)


def register(client, name="Ana Reyes", email="ana@example.com", program="BSIS", campus="Taculing", year="1", consent=True):
    d = {"name": name, "email": email, "program": program, "campus": campus, "year_level": year}
    if consent:
        d["consent"] = "on"
    return post(client, "/register", d)


def upload(client, doc, content=PNG, filename="f.png"):
    return post(client, "/student/upload", {"doc_type": doc, "file": (io.BytesIO(content), filename)},
                content_type="multipart/form-data")


def registrar(app):
    c = app.test_client()
    post(c, "/registrar/login", {"password": "pw"})
    return c


def q(app, sql, *args):
    with app.app_context():
        return get_db().execute(sql, args).fetchall()


def ref_of(app, email="ana@example.com"):
    return q(app, "SELECT ref FROM students WHERE email=?", email)[0]["ref"]


def sec_id(app, code, section="A"):
    return q(app, "SELECT sec.id FROM sections sec JOIN subjects s ON s.id=sec.subject_id WHERE s.code=? AND sec.section_code=?",
             code, section)[0]["id"]


def sub_id(app, code):
    return q(app, "SELECT id FROM subjects WHERE code=?", code)[0]["id"]


def advise(client, app, picks):
    data = {f"sec_{sub_id(app, c)}": sec_id(app, c, s) for c, s in picks}
    return post(client, "/student/advising", data)


def page(client, url):
    return client.get(url, follow_redirects=True).get_data(as_text=True)


def accept_all_docs(app, ref):
    reg = registrar(app)
    for r in q(app, "SELECT d.id FROM documents d JOIN students s ON s.id=d.student_id WHERE s.ref=?", ref):
        post(reg, f"/registrar/doc/{r['id']}/accept")
    return reg


def full_enroll(client, app, email="ana@example.com", plan="full", pay=None):
    register(client, email=email)
    for d in DOCS:
        upload(client, d, PDF, "x.pdf")
    ref = ref_of(app, email)
    reg = accept_all_docs(app, ref)
    advise(client, app, [("GE 101", "A"), ("GE 102", "A"), ("IS 101", "A")])
    post(reg, f"/registrar/student/{ref}/advising/approve")
    post(client, "/student/payment", {"plan": plan, "method": "cashier"})
    from app import summary, student_by_ref
    with app.app_context():
        s = summary(student_by_ref(ref))
        amount = pay if pay is not None else s["required"]
    post(reg, f"/registrar/student/{ref}/payment", {"amount": amount, "method": "cashier"})
    return ref, reg


# ======================= registration and documents (BCC-UERS) =======================
def test_register_creates_student_and_reference(client, app):
    assert register(client).status_code == 302 and ref_of(app) == "BCC-2027-0001"


def test_register_requires_consent(client):
    assert register(client, consent=False).status_code == 400


def test_register_rejects_bad_email_program_and_year(client):
    assert register(client, email="not-an-email").status_code == 400
    assert register(client, program="BSFAKE").status_code == 400
    assert register(client, year="9").status_code == 400


def test_duplicate_email_rejected(client):
    register(client)
    assert register(client, name="Other Person").status_code == 400


def test_form_without_csrf_token_is_refused(client):
    assert client.post("/register", data={"name": "x"}).status_code == 400


def test_html_in_name_is_escaped(client):
    register(client, name="<script>alert(1)</script>")
    out = client.get("/student").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in out and "&lt;script&gt;" in out


def test_valid_upload_is_submitted(client):
    register(client)
    assert upload(client, DOCS[0]).status_code == 302
    assert "submitted" in client.get("/student").get_data(as_text=True)


def test_wrong_extension_fake_image_and_oversize_are_refused(client):
    register(client)
    upload(client, DOCS[0], PNG, "virus.exe")
    assert "Only real PDF, JPG or PNG" in page(client, "/student")
    upload(client, DOCS[0], b"just text", "fake.png")
    assert "Only real PDF, JPG or PNG" in page(client, "/student")
    assert upload(client, DOCS[0], PNG + b"0" * (2 * 1024 * 1024), "big.png").status_code == 413


def test_registrar_pages_need_login(client):
    for url in ("/registrar", "/registrar/verify", "/registrar/audit", "/registrar/subjects", "/registrar/sections",
                "/registrar/fees", "/registrar/reports", "/registrar/export/students.csv", "/registrar/students/new"):
        assert client.get(url).status_code == 302, url


def test_wrong_registrar_password_rejected(client):
    r = post(client, "/registrar/login", {"password": "nope"})
    assert r.status_code == 200 and b"Wrong password" in r.data


def test_accepted_documents_alone_do_not_make_a_student_enrolled(client, app):
    register(client)
    for d in DOCS:
        upload(client, d, PDF, "x.pdf")
    ref = ref_of(app)
    reg = registrar(app)
    assert "Documents under review" in page(reg, f"/registrar/student/{ref}")
    accept_all_docs(app, ref)
    out = page(reg, f"/registrar/student/{ref}")
    assert 'tag warn">Needs subject advising' in out and 'tag ok">Officially enrolled' not in out


def test_rejected_document_can_be_resubmitted(client, app):
    register(client)
    upload(client, DOCS[1], PNG, "id.png")
    reg = registrar(app)
    did = q(app, "SELECT id FROM documents")[0]["id"]
    post(reg, f"/registrar/doc/{did}/reject", {"note": "Blurry"})
    out = page(client, "/student")
    assert "Action needed: document" in out and "Blurry" in out
    upload(client, DOCS[1], PNG, "id2.png")
    assert "Action needed" not in page(client, "/student")


# ======================= QR verification =======================
def token_of(app, email="ana@example.com"):
    with app.test_request_context():
        return make_token(ref_of(app, email))


def test_valid_qr_shows_stage(client, app):
    register(client)
    r = post(registrar(app), "/registrar/verify", {"token": token_of(app)})
    assert b"Valid code" in r.data and b"Ana Reyes" in r.data and b"Documents incomplete" in r.data


def test_tampered_and_expired_codes_are_rejected(client, app):
    register(client)
    reg = registrar(app)
    ref, exp, sig = token_of(app).split(".")
    assert b"altered or forged" in post(reg, "/registrar/verify", {"token": f"{ref}.{int(exp) + 999999}.{sig}"}).data
    with app.test_request_context():
        old = int(time.time()) - 10
        tok = f"{ref}.{old}.{_sig(ref, old)}"
    assert b"expired" in post(reg, "/registrar/verify", {"token": tok}).data
    assert b"Not a BCC code" in post(reg, "/registrar/verify", {"token": "hello"}).data


def test_verification_is_fast_and_logged(client, app):
    register(client)
    reg = registrar(app)
    r = post(reg, "/registrar/verify", {"token": token_of(app)})
    ms = float(r.get_data(as_text=True).split("Valid code</b> (")[1].split(" ms")[0])
    assert ms < 1000 and "verified code for" in page(reg, "/registrar/audit")


def test_student_cannot_see_another_students_qr(client, app):
    register(client)
    other = app.test_client()
    register(other, name="Ben Cruz", email="ben@example.com", program="BEED", campus="Sum-ag")
    assert other.get(f"/qr/{ref_of(app)}.png").status_code == 403


# ======================= registrar account and access =======================
def test_registrar_account_has_no_student_pages(app):
    reg = registrar(app)
    out = reg.get("/registrar").get_data(as_text=True)
    assert 'href="/lookup"' not in out and ">Register<" not in out and "Verify QR" in out
    assert reg.get("/").status_code == 302 and reg.get("/lookup").status_code == 302
    post(reg, "/register", {"name": "X Y", "email": "x@example.com", "program": "BSIS", "campus": "Taculing", "consent": "on"})
    assert q(app, "SELECT COUNT(*) n FROM students")[0]["n"] == 0


def test_student_sees_student_links_only(client):
    out = client.get("/").get_data(as_text=True)
    assert 'href="/lookup"' in out and "Dashboard" not in out
    register(client)
    out = client.get("/student").get_data(as_text=True)
    assert "Subjects" in out and "Payment" in out and "Dashboard" not in out


def test_student_cannot_open_registrar_pages(client):
    register(client)
    assert client.get("/registrar/reports").status_code == 302


def test_audit_log_records_actions(client, app):
    register(client)
    upload(client, DOCS[0])
    out = page(registrar(app), "/registrar/audit")
    assert "registered" in out and "uploaded" in out and "logged in" in out


# ======================= curriculum, sections, fees (2009 study: schedules) =======================
def test_sample_curriculum_loads(app):
    assert q(app, "SELECT COUNT(*) n FROM subjects")[0]["n"] == 12
    assert q(app, "SELECT COUNT(*) n FROM sections")[0]["n"] >= 12
    assert q(app, "SELECT COUNT(*) n FROM fees")[0]["n"] == 2


def test_advising_shows_only_subjects_for_the_students_program_and_year(client):
    register(client, program="BSIS")
    out = client.get("/student/advising").get_data(as_text=True)
    assert "IS 101" in out and "GE 101" in out and "ED 101" not in out


def test_subject_add_validation_and_duplicates(app):
    reg = registrar(app)
    good = {"code": "IS 201", "title": "Data Structures", "units": "3", "program": "BSIS", "year_level": "2", "semester": "1"}
    post(reg, "/registrar/subjects", good)
    assert q(app, "SELECT COUNT(*) n FROM subjects WHERE code='IS 201'")[0]["n"] == 1
    r = post(reg, "/registrar/subjects", good, follow_redirects=True)
    assert b"already exists" in r.data
    r = post(reg, "/registrar/subjects", {**good, "code": "X", "units": "9"}, follow_redirects=True)
    assert b"Units must be" in r.data


def test_subject_csv_import_adds_valid_rows_only(app):
    reg = registrar(app)
    csv_text = ("code,title,units,program,year_level,semester\n"
                "IS 301,Systems Analysis,3,BSIS,3,1\nBAD,No Units,x,BSIS,3,1\nIS 301,Duplicate,3,BSIS,3,1\n")
    r = post(reg, "/registrar/subjects/import", {"file": (io.BytesIO(csv_text.encode()), "s.csv")},
             content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 1 subjects; skipped 2" in r.data


def test_section_add_validation_and_room_conflict(app):
    reg = registrar(app)
    base = {"subject_id": sub_id(app, "GE 102"), "section_code": "C", "days": "MWF", "time_start": "08:30",
            "time_end": "09:30", "room": "Room 101", "capacity": "30"}
    assert b"already used" in post(reg, "/registrar/sections", base, follow_redirects=True).data
    assert b"Days must use" in post(reg, "/registrar/sections", {**base, "days": "Monday", "room": "R9"}, follow_redirects=True).data
    assert b"start before the end" in post(reg, "/registrar/sections", {**base, "time_end": "08:00", "room": "R9"}, follow_redirects=True).data
    post(reg, "/registrar/sections", {**base, "room": "Room 450"})
    assert q(app, "SELECT COUNT(*) n FROM sections WHERE section_code='C'")[0]["n"] == 1
    assert b"already has a section" in post(reg, "/registrar/sections", {**base, "room": "Room 451"}, follow_redirects=True).data


def test_fee_and_tuition_changes_flow_into_the_assessment(client, app):
    register(client)
    advise(client, app, [("GE 101", "A"), ("IS 101", "A")])
    out = page(client, "/student/slip/assessment")
    assert "Tuition (6 units at" in out and "180.00" in out and "430.00" in out   # 6 x 30 + 150 + 100
    reg = registrar(app)
    post(reg, "/registrar/settings", {"school_year": "2027-2028", "semester": "1", "tuition_per_unit": "50", "max_units": "24"})
    assert "430.00" not in page(client, "/student/slip/assessment") and "550.00" in page(client, "/student/slip/assessment")
    fid = q(app, "SELECT id FROM fees")[0]["id"]
    post(reg, f"/registrar/fees/{fid}/delete")
    assert q(app, "SELECT COUNT(*) n FROM fees")[0]["n"] == 1
    assert b"Semester must be" in post(reg, "/registrar/settings", {"school_year": "2027-2028", "semester": "5",
                                      "tuition_per_unit": "50", "max_units": "24"}, follow_redirects=True).data


# ======================= advising rules =======================
def test_advising_submit_sets_status_and_total(client, app):
    register(client)
    advise(client, app, [("GE 101", "A"), ("GE 102", "A"), ("IS 101", "A")])
    assert "Subjects status: <b>submitted</b>" in page(client, "/student")
    assert q(app, "SELECT status FROM advising")[0]["status"] == "submitted"


def test_time_conflict_is_refused(client, app):
    reg = registrar(app)
    post(reg, "/registrar/sections", {"subject_id": sub_id(app, "GE 102"), "section_code": "C", "days": "MWF",
                                      "time_start": "08:30", "time_end": "09:30", "room": "Room 450", "capacity": "30"})
    register(client)
    r = advise(client, app, [("GE 101", "A"), ("GE 102", "C")])
    assert "meet at the same time" in page(client, "/student/advising")
    assert q(app, "SELECT COUNT(*) n FROM enrollments")[0]["n"] == 0


def test_unit_limit_is_enforced(client, app):
    reg = registrar(app)
    post(reg, "/registrar/settings", {"school_year": "2027-2028", "semester": "1", "tuition_per_unit": "30", "max_units": "5"})
    register(client)
    advise(client, app, [("GE 101", "A"), ("GE 102", "A")])
    assert "the limit is 5" in page(client, "/student/advising")
    assert q(app, "SELECT COUNT(*) n FROM enrollments")[0]["n"] == 0


def test_section_capacity_and_seat_release(client, app):
    reg = registrar(app)
    post(reg, "/registrar/sections", {"subject_id": sub_id(app, "GE 101"), "section_code": "Z", "days": "S",
                                      "time_start": "14:00", "time_end": "15:00", "room": "Room 460", "capacity": "1"})
    register(client)
    other = app.test_client()
    register(other, name="Ben Cruz", email="ben@example.com", program="BEED", campus="Sum-ag")
    advise(client, app, [("GE 101", "Z")])
    advise(other, app, [("GE 101", "Z")])
    assert "is full" in page(other, "/student/advising")
    post(reg, f"/registrar/student/{ref_of(app)}/advising/reject", {"note": "Revise"})
    advise(other, app, [("GE 101", "Z")])
    assert q(app, "SELECT COUNT(*) n FROM enrollments e JOIN students s ON s.id=e.student_id WHERE s.email='ben@example.com'")[0]["n"] == 1


def test_student_cannot_take_a_subject_from_another_program(client, app):
    register(client, program="BSIS")
    post(client, "/student/advising", {f"sec_{sub_id(app, 'ED 101')}": sec_id(app, "ED 101")})
    assert q(app, "SELECT COUNT(*) n FROM enrollments")[0]["n"] == 0


def test_approved_subjects_are_locked_until_registrar_reopens(client, app):
    register(client)
    advise(client, app, [("GE 101", "A")])
    reg = registrar(app)
    ref = ref_of(app)
    post(reg, f"/registrar/student/{ref}/advising/approve")
    advise(client, app, [("GE 102", "A")])
    assert q(app, "SELECT COUNT(*) n FROM enrollments")[0]["n"] == 1 and "approved" in page(client, "/student/advising")
    post(reg, f"/registrar/student/{ref}/advising/reject", {"note": "Reopened"})
    advise(client, app, [("GE 102", "A")])
    assert q(app, "SELECT s.code FROM enrollments e JOIN sections sec ON sec.id=e.section_id JOIN subjects s ON s.id=sec.subject_id")[0]["code"] == "GE 102"


def test_only_submitted_subjects_can_be_approved(client, app):
    register(client)
    reg = registrar(app)
    r = post(reg, f"/registrar/student/{ref_of(app)}/advising/approve", follow_redirects=True)
    assert b"Only submitted subjects" in r.data


def test_section_cannot_be_deleted_while_students_are_in_it(client, app):
    register(client)
    advise(client, app, [("GE 101", "A")])
    reg = registrar(app)
    r = post(reg, f"/registrar/section/{sec_id(app, 'GE 101')}/delete", follow_redirects=True)
    assert b"cannot be deleted" in r.data


# ======================= payment (2009 study: payment selection) =======================
def test_payment_needs_subjects_first(client):
    register(client)
    assert b"Choose your subjects first" in client.get("/student/payment", follow_redirects=True).data


def test_payment_choice_validation(client, app):
    register(client)
    advise(client, app, [("GE 101", "A")])
    assert b"Choose a payment plan" in post(client, "/student/payment", {"plan": "bad", "method": "cashier"}, follow_redirects=True).data
    post(client, "/student/payment", {"plan": "installment", "method": "ewallet"})
    assert q(app, "SELECT plan, method FROM payment_plans")[0]["plan"] == "installment"


def test_registrar_payment_validation(client, app):
    register(client)
    advise(client, app, [("GE 101", "A")])
    reg = registrar(app)
    ref = ref_of(app)
    assert b"more than the balance" in post(reg, f"/registrar/student/{ref}/payment", {"amount": "99999", "method": "cashier"}, follow_redirects=True).data
    assert b"above zero" in post(reg, f"/registrar/student/{ref}/payment", {"amount": "abc", "method": "cashier"}, follow_redirects=True).data
    post(reg, f"/registrar/student/{ref}/payment", {"amount": "100", "method": "bank"})
    assert q(app, "SELECT SUM(amount) s FROM payments")[0]["s"] == 100


# ======================= whole lifecycle =======================
def test_full_enrollment_lifecycle_and_qr_proof(client, app):
    ref, reg = full_enroll(client, app)
    assert 'tag ok">Officially enrolled' in page(client, "/student")
    out = post(reg, "/registrar/verify", {"token": token_of(app)}).get_data(as_text=True)
    assert "Valid code" in out and "Officially enrolled" in out
    cert = page(client, "/student/slip/certificate")
    assert "Certificate of Enrollment" in cert and "Ana Reyes" in cert and ref in cert


def test_installment_needs_half_to_enroll_and_full_needs_all(client, app):
    ref, reg = full_enroll(client, app, plan="installment", pay=1)
    assert 'tag ok">Officially enrolled' not in page(client, "/student") and 'tag warn">Awaiting payment' in page(client, "/student")
    from app import summary, student_by_ref
    with app.app_context():
        need = summary(student_by_ref(ref))["required"] - 1
    post(reg, f"/registrar/student/{ref}/payment", {"amount": need, "method": "cashier"})
    assert 'tag ok">Officially enrolled' in page(client, "/student")


def test_certificate_is_blocked_until_enrolled(client, app):
    register(client)
    assert b"officially enrolled" in client.get("/student/slip/certificate", follow_redirects=True).data


def test_slips_show_subjects_and_need_a_selection(client, app):
    register(client)
    assert b"No subjects chosen yet" in client.get("/student/slip/advising", follow_redirects=True).data
    advise(client, app, [("GE 101", "A")])
    out = page(client, "/student/slip/advising")
    assert "GE 101" in out and "MWF 08:00 to 09:00" in out
    assert "GE 101" in page(registrar(app), f"/registrar/student/{ref_of(app)}/slip/advising")


# ======================= records, reports and exports =======================
def test_edit_record_and_program_change_clears_subjects(client, app):
    register(client)
    advise(client, app, [("IS 101", "A")])
    reg = registrar(app)
    ref = ref_of(app)
    post(reg, f"/registrar/student/{ref}/edit", {"name": "Ana R. Reyes", "program": "BEED", "campus": "Sum-ag", "year_level": "1"})
    row = q(app, "SELECT * FROM students")[0]
    assert row["name"] == "Ana R. Reyes" and row["program"] == "BEED" and row["campus"] == "Sum-ag"
    assert q(app, "SELECT COUNT(*) n FROM enrollments")[0]["n"] == 0
    assert "edited record" in page(reg, "/registrar/audit")


def test_walk_in_record_needs_consent(app):
    reg = registrar(app)
    base = {"name": "Walk In", "email": "walk@example.com", "program": "BSOA", "campus": "Taculing", "year_level": "1"}
    assert post(reg, "/registrar/students/new", base).status_code == 400
    r = post(reg, "/registrar/students/new", {**base, "consent": "on"})
    assert r.status_code == 302 and q(app, "SELECT COUNT(*) n FROM students")[0]["n"] == 1
    assert "added walk-in record" in page(reg, "/registrar/audit")


def test_reports_and_dashboard_numbers(client, app):
    full_enroll(client, app)
    reg = registrar(app)
    dash = page(reg, "/registrar")
    assert "officially enrolled" in dash and "Collected" in dash
    rep = page(reg, "/registrar/reports")
    assert "Enrollment by program and year" in rep and "BSIS" in rep


def test_csv_exports_and_formula_injection_guard(client, app):
    register(client, name="=HYPERLINK(1)")
    advise(client, app, [("GE 101", "A")])
    reg = registrar(app)
    students = reg.get("/registrar/export/students.csv")
    assert students.mimetype == "text/csv" and "ref,name,email" in students.get_data(as_text=True)
    assert "'=HYPERLINK(1)" in students.get_data(as_text=True)
    assert "GE 101" in reg.get("/registrar/export/enrollments.csv").get_data(as_text=True)
    assert reg.get("/registrar/export/payments.csv").status_code == 200
    assert "ref,name" in reg.get(f"/registrar/export/roster/{sec_id(app, 'GE 101')}.csv").get_data(as_text=True)
    assert reg.get("/registrar/export/nothing.csv").status_code == 404


def test_old_database_without_year_level_is_upgraded(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE students(id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, name TEXT NOT NULL, "
                "email TEXT UNIQUE NOT NULL, program TEXT NOT NULL, campus TEXT NOT NULL, consent INTEGER NOT NULL, created TEXT NOT NULL)")
    con.commit()
    con.close()
    create_app({"TESTING": True, "DATABASE": str(db), "UPLOAD_DIR": str(tmp_path / "u")})
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(students)")]
    assert "year_level" in cols
