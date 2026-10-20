# BCC-UERS: simple working system (evidence build)

Unified enrollment for Bacolod City College: one registration form for both campuses, online document
submission with registrar review, signed QR codes for enrollment verification, a registrar dashboard
and an audit log. Flask + SQLite. This is a small working system for the proposal, not a finished
production product.

## Run it
```
python -m venv venv
venv\Scripts\activate          (Windows)   or   source venv/bin/activate
pip install -r requirements.txt
flask --app app run
```
Open http://127.0.0.1:5000
- Students: Register, then upload documents. "Check my status" needs the reference number and email.
- Registrar: Registrar link, default password `registrar123`. Change it before real use:
  set `REGISTRAR_PASSWORD` and `SECRET_KEY` environment variables.

## Run the tests and regenerate the evidence
```
python -m pytest -v tests          (21 tests)
python make_evidence.py            (needs: pip install playwright && playwright install chromium)
```
`make_evidence.py` drives the real site with a browser and writes the `evidence/` folder:
`run_log.txt`, `test_results.txt`, `sample_qr_code.png`, and `screenshots/` (register, student page with QR,
fake file refused, dashboard, registrar review, valid/tampered/garbage code checks, audit log).

## Where each proposal objective is shown
| Proposal item | Where in the system | Evidence |
|---|---|---|
| One shared database for CoEd and non-CoEd forms | `/` register form, `students` table | screenshot 01, test_register_creates_student |
| Online document submission with status | `/student`, `/student/upload`, registrar `/registrar/student/<ref>` | screenshots 02, 03, 05; upload tests |
| QR verification under 10 seconds | `/qr/<ref>.png`, `/registrar/verify` | run_log (0.34 ms check), test_verification_is_fast_and_logged |
| Forged or expired codes refused | `check_token` in app.py (HMAC-SHA256) | screenshots 06, tampered and expired tests |
| Registrar dashboard across both campuses | `/registrar` | screenshot 04 |
| RA 10173 controls | consent box, role login, audit log, file content check, CSRF token, escaped output | screenshot 07, tests |

## Limits to state honestly in the defense
- SQLite and local file storage: fine for a pilot, move to PostgreSQL and cloud storage for production.
- One shared registrar password and no lockout. Production needs individual accounts and rate limiting.
- Uploaded files are checked by extension, size and file signature, not scanned for malware.
- The QR code holds a signed token; the registrar pastes the scanned text into the verify page. A
  camera scanner page is a next step.
- Run behind HTTPS and set `COOKIE_SECURE=1` before any real deployment.
- The 0.34 ms verification time is server lookup time on a laptop, not the whole counter transaction.
