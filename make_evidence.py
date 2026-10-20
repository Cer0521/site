"""Runs the system end to end and saves evidence: run log, sample QR image, screenshots, test results."""
import io, os, shutil, subprocess, sys, tempfile, threading, time
from werkzeug.serving import make_server
from app import DOCS, create_app, make_token

EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
SHOTS = os.path.join(EVID, "screenshots")
shutil.rmtree(EVID, ignore_errors=True)
os.makedirs(SHOTS)
tmp = tempfile.mkdtemp()
app = create_app({"DATABASE": os.path.join(tmp, "demo.db"), "UPLOAD_DIR": os.path.join(tmp, "up"),
                  "SECRET_KEY": "evidence-run-key", "REGISTRAR_PASSWORD": "registrar123"})
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 256
log = []
def say(t):
    line = f"[{time.strftime('%H:%M:%S')}] {t}"
    print(line); log.append(line)

srv = make_server("127.0.0.1", 5055, app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:5055"

from playwright.sync_api import sync_playwright
STUDENTS = [("Ana Reyes", "BSIS", "Taculing", 4), ("Ben Cruz", "BEED", "Sum-ag", 4),
            ("Cathy Lim", "BSBA-MM", "Taculing", 2), ("Dan Uy", "BSOA", "Sum-ag", 1), ("Eli Tan", "BSED", "Taculing", 3)]
with sync_playwright() as p:
    b = p.chromium.launch()
    reg = b.new_context(viewport={"width": 1000, "height": 760}).new_page()
    refs = {}
    for i, (name, prog, camp, ndocs) in enumerate(STUDENTS):
        ctx = b.new_context(viewport={"width": 1000, "height": 760}); pg = ctx.new_page()
        pg.goto(BASE + "/")
        if i == 0:
            pg.screenshot(path=f"{SHOTS}/01_register_form.png")
        pg.fill("#name", name); pg.fill("#email", name.split()[0].lower() + "@example.com")
        pg.select_option("#program", prog); pg.select_option("#campus", camp); pg.check("input[name=consent]")
        pg.click("button"); pg.wait_for_url("**/student")
        ref = pg.inner_text("main .card p").split("\n")[0]; refs[name] = ref
        say(f"Registered {name} -> {ref} ({prog}, {camp})")
        for d in DOCS[:ndocs]:
            pg.locator("tr", has_text=d).locator("input[type=file]").set_input_files(
                files=[{"name": "scan.png", "mimeType": "image/png", "buffer": PNG}])
            pg.locator("tr", has_text=d).locator("button").click(); pg.wait_for_load_state()
        say(f"  uploaded {ndocs} of {len(DOCS)} documents")
        if i == 0:
            pg.screenshot(path=f"{SHOTS}/02_student_page_with_qr.png", full_page=True)
            # a fake file is refused
            pg.locator("tr", has_text=DOCS[3]).locator("input[type=file]").set_input_files(
                files=[{"name": "fake.png", "mimeType": "image/png", "buffer": b"not an image"}])
            pg.locator("tr", has_text=DOCS[3]).locator("button").click(); pg.wait_for_load_state()
            say("  tried uploading a fake PNG -> refused: " + pg.inner_text(".msg"))
            pg.screenshot(path=f"{SHOTS}/03_fake_file_refused.png", full_page=True)
        ctx.close()

    r = b.new_context(viewport={"width": 1000, "height": 760}).new_page()
    r.goto(BASE + "/registrar/login"); r.fill("#password", "registrar123"); r.click("button"); r.wait_for_url("**/registrar")
    say("Registrar signed in")
    # accept all of Ana's and Ben's submitted docs, reject one of Cathy's
    for name in ("Ana Reyes", "Ben Cruz"):
        r.goto(f"{BASE}/registrar/student/{refs[name]}")
        for i in range(4):
            r.locator("form[action$='/accept'] button").nth(i).click(); r.wait_for_load_state()
        say(f"Accepted all 4 documents for {name}")
    r.goto(f"{BASE}/registrar/student/{refs['Cathy Lim']}")
    r.locator("form[action$='/reject'] input[name=note]").first.fill("ID is blurry")
    r.locator("form[action$='/reject'] button").first.click(); r.wait_for_load_state()
    say("Rejected one of Cathy Lim's documents (reason: ID is blurry)")
    r.screenshot(path=f"{SHOTS}/05_registrar_reviews_documents.png", full_page=True)

    with app.app_context():
        good = make_token(refs["Ana Reyes"])
    ref, exp, sig = good.split(".")
    for label, tok in (("valid code", good), ("tampered code", f"{ref}.{int(exp)+99999}.{sig}"), ("garbage text", "hello")):
        r.goto(BASE + "/registrar/verify"); r.fill("#token", tok); r.click("button"); r.wait_for_load_state()
        res = " ".join(r.inner_text("[role=status]").split())
        say(f"Verified {label}: {res}")
        r.screenshot(path=f"{SHOTS}/06_verify_{label.split()[0]}.png", full_page=True)
    for _ in range(25):
        r.goto(BASE + "/registrar/verify"); r.fill("#token", good); r.click("button"); r.wait_for_load_state()
    r.goto(BASE + "/registrar"); r.screenshot(path=f"{SHOTS}/04_dashboard.png", full_page=True)
    cards = [" ".join(t.split()) for t in r.locator(".card").all_inner_texts()[:5]]
    say("Dashboard cards: " + " | ".join(cards))
    r.goto(BASE + "/registrar/audit"); r.screenshot(path=f"{SHOTS}/07_audit_log.png", full_page=True)
    b.close()

with app.app_context():
    import qrcode
    qrcode.make(make_token(refs["Ana Reyes"])).save(f"{EVID}/sample_qr_code.png")
srv.shutdown()
open(f"{EVID}/run_log.txt", "w").write("BCC-UERS end to end run\n" + "\n".join(log) + "\n")
t = subprocess.run([sys.executable, "-m", "pytest", "-v", "tests"], capture_output=True, text=True)
open(f"{EVID}/test_results.txt", "w").write(t.stdout)
print(t.stdout.splitlines()[-1])
