# BCC-UERS: whole enrollment system

One working system that combines two studies:
- **The main study**: Online Enrollment System for Cavite Maritime Institute (2009), which digitized student records,
  subject and schedule viewing, advising, assessment of fees, payment choice and reports.
- **BCC-UERS (this study)**: unified registration, online document submission, registrar review, signed QR
  verification, dashboard, audit log and data privacy controls.

Flask + SQLite. A small, working pilot for the Bacolod City College proposal, not a finished production product.

## Run it (any PC with Python 3.10 or newer)
```
python -m venv venv
venv\Scripts\activate          (Windows)   or   source venv/bin/activate
pip install -r requirements.txt
flask --app app seed-demo      (loads a SAMPLE curriculum, sections and fees)
flask --app app run
```
Open http://127.0.0.1:5000. The database and uploads are created in the `instance/` folder on first run,
so a new PC starts empty. Copy `instance/` too if you want the same data.
- Registrar: Registrar link, default password `registrar123`. Set `REGISTRAR_PASSWORD` and `SECRET_KEY`
  environment variables before real use.
- The seed data is a sample. Replace it with BCC's approved curriculum on the registrar's Subjects page
  (add one by one, or import a CSV).

## The enrollment process in the system
1. Student registers (one form, all programs and campuses) and uploads documents.
2. Registrar accepts or rejects each document (with a reason).
3. Student chooses subjects and sections (conflicts, seat limits and unit load are checked).
4. Registrar approves the subjects.
5. Fees are assessed automatically; student picks a payment plan and method; registrar records payments.
6. When documents, subjects and payment are all complete the student is **Officially enrolled** and can print a
   certificate whose QR code the registrar can verify.

## Where each feature comes from
| Feature | From | Where |
|---|---|---|
| Student records (add, update) | 2009 study | Registrar: New record, Edit record |
| Subject and schedule viewing | 2009 study | Student: Subjects; Registrar: Subjects, Sections |
| Class scheduling and sectioning | 2009 study | Registrar: Sections (room and time conflict checks, seat limits) |
| Automated advising slip | 2009 study | Student and registrar: Advising slip |
| Assessment of fees | 2009 study | Fees page, Assessment slip |
| Online payment selection | 2009 study | Student: Payment (choice recorded; cashier confirms payment) |
| Report generation | 2009 study | Registrar: Reports and CSV downloads |
| Unified registration | BCC-UERS | Register page |
| Online document submission | BCC-UERS | Student page, registrar review |
| Signed QR verification | BCC-UERS | Certificate, Verify QR |
| Dashboard with objective checks | BCC-UERS | Registrar: Dashboard |
| Audit log and privacy controls | BCC-UERS | Audit log, consent, file checks, form tokens |

## Tests and evidence
```
python -m pytest -v tests          (46 tests)
python make_evidence.py            (needs: pip install playwright && playwright install chromium)
```
`make_evidence.py` drives the real site in a browser and writes `evidence/`: `run_log.txt`, `test_results.txt`,
`sample_qr_code.png` and 17 screenshots of the whole process.

## Limits to state honestly
- Payment is a choice plus a record kept by the registrar. There is no payment gateway.
- The sample curriculum and sample fees are demonstration data. Tuition defaults to 30 pesos per unit, the rate
  BCC publishes on its About page; confirm it with the Registrar.
- SQLite and local file storage suit a pilot. Use PostgreSQL and cloud storage for production.
- One shared registrar password and no lockout; individual accounts and rate limiting are next steps.
- Uploaded files are checked by extension, size and signature, not scanned for malware.
- The registrar pastes the scanned QR text into the verify page; a camera scanner page is a next step.
- Run behind HTTPS and set `COOKIE_SECURE=1` before any real deployment.
