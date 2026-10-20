import io
import time

import pytest

from app import DOCS, create_app, get_db

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


@pytest.fixture()
def app(tmp_path):
    a = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "UPLOAD_DIR": str(tmp_path / "up"),
                    "SECRET_KEY": "test-key", "REGISTRAR_PASSWORD": "pw"})
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


def register(client, name="Ana Reyes", email="ana@example.com", program="BSIS", campus="Taculing", consent=True):
    d = {"name": name, "email": email, "program": program, "campus": campus}
    if consent:
        d["consent"] = "on"
    return post(client, "/register", d)


def upload(client, doc, content=PNG, filename="f.png"):
    return post(client, "/student/upload", {"doc_type": doc, "file": (io.BytesIO(content), filename)},
                content_type="multipart/form-data")


def login(client):
    return post(client, "/registrar/login", {"password": "pw"})


def ref_of(app, email="ana@example.com"):
    with app.app_context():
        return get_db().execute("SELECT ref FROM students WHERE email=?", (email,)).fetchone()["ref"]


def token_of(client, app):
    # registrar session fetches the QR image; the token itself is rebuilt the same way the app does
    from app import make_token
    with app.test_request_context():
        return make_token(ref_of(app))


# ---- registration ----
def test_register_creates_student_and_reference(client, app):
    r = register(client)
    assert r.status_code == 302 and ref_of(app) == "BCC-2027-0001"


def test_register_requires_consent(client):
    assert register(client, consent=False).status_code == 400


def test_register_rejects_bad_email_and_program(client):
    assert register(client, email="not-an-email").status_code == 400
    assert register(client, program="BSFAKE").status_code == 400


def test_duplicate_email_rejected(client):
    register(client)
    assert register(client, name="Other Person").status_code == 400


def test_form_without_csrf_token_is_refused(client):
    assert client.post("/register", data={"name": "x"}).status_code == 400


def test_html_in_name_is_escaped(client):
    register(client, name="<script>alert(1)</script>")
    page = client.get("/student").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


# ---- documents ----
def test_valid_upload_is_accepted_as_submitted(client, app):
    register(client)
    assert upload(client, DOCS[0]).status_code == 302
    assert "submitted" in client.get("/student").get_data(as_text=True)


def test_wrong_extension_rejected(client):
    register(client)
    upload(client, DOCS[0], PNG, "virus.exe")
    assert "submitted" not in client.get("/student").get_data(as_text=True).split("Requirement")[1]


def test_fake_image_rejected_by_content_check(client):
    register(client)
    upload(client, DOCS[0], b"just text pretending", "fake.png")
    assert "Only real PDF, JPG or PNG" in client.get("/student").get_data(as_text=True)


def test_oversize_upload_rejected(client):
    register(client)
    r = upload(client, DOCS[0], PNG + b"0" * (2 * 1024 * 1024), "big.png")
    assert r.status_code == 413


# ---- registrar ----
def test_registrar_pages_need_login(client):
    for url in ("/registrar", "/registrar/verify", "/registrar/audit"):
        assert client.get(url).status_code == 302


def test_wrong_registrar_password_rejected(client):
    r = post(client, "/registrar/login", {"password": "nope"})
    assert r.status_code == 200 and b"Wrong password" in r.data


def test_enrolled_only_when_all_documents_accepted(client, app):
    register(client)
    for d in DOCS:
        upload(client, d, PDF, "x.pdf")
    reg = app.test_client()
    login(reg)
    ref = ref_of(app)
    assert "Under review" in reg.get(f"/registrar/student/{ref}").get_data(as_text=True)
    with app.app_context():
        ids = [r["id"] for r in get_db().execute("SELECT id FROM documents")]
    for i in ids:
        post(reg, f"/registrar/doc/{i}/accept")
    assert "Enrolled" in reg.get(f"/registrar/student/{ref}").get_data(as_text=True)


def test_reject_shows_action_needed_and_student_can_resubmit(client, app):
    register(client)
    upload(client, DOCS[1], PNG, "id.png")
    reg = app.test_client()
    login(reg)
    with app.app_context():
        did = get_db().execute("SELECT id FROM documents").fetchone()["id"]
    post(reg, f"/registrar/doc/{did}/reject", {"note": "Blurry"})
    page = client.get("/student").get_data(as_text=True)
    assert "Action needed" in page and "Blurry" in page
    upload(client, DOCS[1], PNG, "id2.png")
    assert "Action needed" not in client.get("/student").get_data(as_text=True)


# ---- QR verification ----
def test_valid_qr_code_verifies_and_shows_status(client, app):
    register(client)
    reg = app.test_client()
    login(reg)
    r = post(reg, "/registrar/verify", {"token": token_of(client, app)})
    assert b"Valid code" in r.data and b"Ana Reyes" in r.data


def test_tampered_qr_code_is_rejected(client, app):
    register(client)
    reg = app.test_client()
    login(reg)
    ref, exp, sig = token_of(client, app).split(".")
    r = post(reg, "/registrar/verify", {"token": f"{ref}.{int(exp) + 999999}.{sig}"})
    assert b"altered or forged" in r.data


def test_expired_qr_code_is_rejected(client, app):
    register(client)
    reg = app.test_client()
    login(reg)
    from app import _sig
    ref = ref_of(app)
    with app.test_request_context():
        exp = int(time.time()) - 10
        tok = f"{ref}.{exp}.{_sig(ref, exp)}"
    assert b"expired" in post(reg, "/registrar/verify", {"token": tok}).data


def test_verification_is_fast_and_logged(client, app):
    register(client)
    reg = app.test_client()
    login(reg)
    r = post(reg, "/registrar/verify", {"token": token_of(client, app)})
    ms = float(r.get_data(as_text=True).split("Valid code</b> (")[1].split(" ms")[0])
    assert ms < 1000
    assert "verified code for" in reg.get("/registrar/audit").get_data(as_text=True)


def test_student_cannot_see_another_students_qr(client, app):
    register(client)
    other = app.test_client()
    post(other, "/register", {"name": "Ben Cruz", "email": "ben@example.com", "program": "BEED",
                              "campus": "Sum-ag", "consent": "on"})
    assert other.get(f"/qr/{ref_of(app)}.png").status_code == 403


def test_audit_log_records_actions(client, app):
    register(client)
    upload(client, DOCS[0])
    reg = app.test_client()
    login(reg)
    page = reg.get("/registrar/audit").get_data(as_text=True)
    assert "registered" in page and "uploaded" in page and "logged in" in page


def test_dashboard_counts(client, app):
    register(client)
    reg = app.test_client()
    login(reg)
    page = reg.get("/registrar").get_data(as_text=True)
    assert "students registered" in page and "1</div>students registered" in page.replace("\n", "")
