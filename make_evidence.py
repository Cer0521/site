"""Runs the whole system end to end in a real browser and saves evidence:
run log, sample QR image, screenshots, and the test results."""
import os, shutil, subprocess, sys, tempfile, threading, time
from werkzeug.serving import make_server
from academics import seed_demo
from app import DOCS, create_app, get_db, make_token, student_by_ref, summary

EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
SHOTS = os.path.join(EVID, "screenshots")
shutil.rmtree(EVID, ignore_errors=True)
os.makedirs(SHOTS)
tmp = tempfile.mkdtemp()
app = create_app({"DATABASE": os.path.join(tmp, "demo.db"), "UPLOAD_DIR": os.path.join(tmp, "up"),
                  "SECRET_KEY": "evidence-run-key", "REGISTRAR_PASSWORD": "registrar123"})
with app.app_context():
    seed_demo()
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 256
log = []
def say(t):
    line = f"[{time.strftime('%H:%M:%S')}] {t}"
    print(line); log.append(line)

def ids(code, section="A"):
    with app.app_context():
        r = get_db().execute("SELECT s.id sub, sec.id sec FROM sections sec JOIN subjects s ON s.id=sec.subject_id "
                             "WHERE s.code=? AND sec.section_code=?", (code, section)).fetchone()
        return r["sub"], r["sec"]

srv = make_server("127.0.0.1", 5055, app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:5055"
from playwright.sync_api import sync_playwright

STUDENTS = [("Ana Reyes", "BSIS", "Taculing", 1, 4), ("Ben Cruz", "BEED", "Sum-ag", 1, 4),
            ("Cathy Lim", "BSBA-MM", "Taculing", 1, 2), ("Dan Uy", "BSOA", "Sum-ag", 1, 1), ("Eli Tan", "BSED", "Taculing", 1, 3)]
SUBJECTS = {"Ana Reyes": [("GE 101", "A"), ("GE 102", "A"), ("IS 101", "A")],
            "Ben Cruz": [("GE 101", "B"), ("GE 103", "B"), ("ED 101", "A")]}

def shot(pg, name, full=True):
    pg.screenshot(path=f"{SHOTS}/{name}.png", full_page=full)

with sync_playwright() as p:
    b = p.chromium.launch()
    pages, refs = {}, {}
    for i, (name, prog, camp, year, ndocs) in enumerate(STUDENTS):
        pg = b.new_context(viewport={"width": 1000, "height": 760}).new_page(); pages[name] = pg
        pg.goto(BASE + "/")
        if i == 0:
            shot(pg, "01_register_form")
        pg.fill("#name", name); pg.fill("#email", name.split()[0].lower() + "@example.com")
        pg.select_option("#program", prog); pg.select_option("#year_level", str(year)); pg.select_option("#campus", camp)
        pg.check("input[name=consent]"); pg.click("button"); pg.wait_for_url("**/student")
        refs[name] = pg.inner_text("main .card p").split("\n")[0]
        say(f"Registered {name} -> {refs[name]} ({prog}, year {year}, {camp})")
        for d in DOCS[:ndocs]:
            pg.locator("tr", has_text=d).locator("input[type=file]").set_input_files(
                files=[{"name": "scan.png", "mimeType": "image/png", "buffer": PNG}])
            pg.locator("tr", has_text=d).locator("button").click(); pg.wait_for_load_state()
        say(f"  uploaded {ndocs} of {len(DOCS)} documents")
        if i == 0:
            shot(pg, "02_student_page_progress_and_qr")
            pg.locator("tr", has_text=DOCS[3]).locator("input[type=file]").set_input_files(
                files=[{"name": "fake.png", "mimeType": "image/png", "buffer": b"not an image"}])
            pg.locator("tr", has_text=DOCS[3]).locator("button").click(); pg.wait_for_load_state()
            say("  tried uploading a fake PNG, refused: " + pg.inner_text(".msg"))
            shot(pg, "03_fake_file_refused")

    r = b.new_context(viewport={"width": 1000, "height": 760}).new_page()
    r.goto(BASE + "/registrar/login"); r.fill("#password", "registrar123"); r.click("button"); r.wait_for_url("**/registrar")
    say("Registrar signed in (menu shows registrar tools only)")
    for name in ("Ana Reyes", "Ben Cruz"):
        r.goto(f"{BASE}/registrar/student/{refs[name]}")
        for i in range(4):
            r.locator("form[action$='/accept'] button").nth(i).click(); r.wait_for_load_state()
        say(f"Accepted all 4 documents for {name}")
    r.goto(f"{BASE}/registrar/student/{refs['Cathy Lim']}")
    r.locator("form[action$='/reject'] input[name=note]").first.fill("ID is blurry")
    r.locator("form[action$='/reject'] button").first.click(); r.wait_for_load_state()
    say("Rejected one of Cathy Lim's documents (reason: ID is blurry)")

    for name, picks in SUBJECTS.items():
        pg = pages[name]
        pg.goto(BASE + "/student/advising")
        if name == "Ana Reyes":
            shot(pg, "04_advising_choose_subjects")
        for code, sec in picks:
            sub, sid = ids(code, sec)
            pg.check(f"input[name=sec_{sub}][value='{sid}']")
        pg.click("button:has-text('Submit subjects')"); pg.wait_for_load_state()
        say(f"{name} chose subjects {', '.join(c + ' ' + s for c, s in picks)}: {pg.inner_text('.msg')}")
        if name == "Ana Reyes":
            pg.goto(BASE + "/student/slip/advising"); shot(pg, "05_advising_slip")
        pg.goto(BASE + "/student/payment")
        plan = "full" if name == "Ana Reyes" else "installment"
        pg.check(f"input[name=plan][value={plan}]"); pg.check("input[name=method][value=cashier]")
        pg.click("button:has-text('Save my choice')"); pg.wait_for_load_state()
        say(f"{name} chose payment plan: {plan} at the cashier")
        if name == "Ana Reyes":
            shot(pg, "06_payment_page")
        r.goto(f"{BASE}/registrar/student/{refs[name]}")
        r.click("button:has-text('Approve subjects')"); r.wait_for_load_state()
        with app.app_context():
            amount = summary(student_by_ref(refs[name]))["required"]
        r.fill("input[name=amount]", str(amount)); r.click("button:has-text('Record payment')"); r.wait_for_load_state()
        say(f"Registrar approved subjects for {name} and recorded payment of PHP {amount:,.2f}")
        if name == "Ana Reyes":
            shot(r, "07_registrar_student_record")

    pg = pages["Ana Reyes"]; pg.goto(BASE + "/student"); shot(pg, "08_student_officially_enrolled")
    pg.goto(BASE + "/student/slip/certificate"); shot(pg, "09_certificate_of_enrollment")
    say("Ana Reyes is officially enrolled; certificate issued with QR code")

    with app.app_context():
        good = make_token(refs["Ana Reyes"])
    ref, exp, sig = good.split(".")
    for label, tok in (("valid", good), ("tampered", f"{ref}.{int(exp)+99999}.{sig}"), ("garbage", "hello")):
        r.goto(BASE + "/registrar/verify"); r.fill("#token", tok); r.click("button:has-text('Verify')"); r.wait_for_load_state()
        say(f"QR check ({label} code): " + " ".join(r.inner_text("[role=status]").split()))
        shot(r, f"10_qr_check_{label}")
    for _ in range(25):
        r.goto(BASE + "/registrar/verify"); r.fill("#token", good); r.click("button:has-text('Verify')"); r.wait_for_load_state()
    r.goto(BASE + "/registrar"); shot(r, "11_dashboard")
    cards = [" ".join(t.split()) for t in r.locator(".card").all_inner_texts()[:8]]
    say("Dashboard cards: " + " | ".join(cards))
    r.goto(BASE + "/registrar/reports"); shot(r, "12_reports")
    r.goto(BASE + "/registrar/sections"); shot(r, "13_class_sections")
    r.goto(BASE + "/registrar/fees"); shot(r, "14_term_and_fees")
    r.goto(BASE + "/registrar/audit"); shot(r, "15_audit_log")
    b.close()

with app.app_context():
    import qrcode
    qrcode.make(make_token(refs["Ana Reyes"])).save(f"{EVID}/sample_qr_code.png")
srv.shutdown()
open(f"{EVID}/run_log.txt", "w").write("BCC-UERS whole system, end to end run\n" + "\n".join(log) + "\n")
t = subprocess.run([sys.executable, "-m", "pytest", "-v", "tests"], capture_output=True, text=True)
open(f"{EVID}/test_results.txt", "w").write(t.stdout)
print(t.stdout.splitlines()[-1])
