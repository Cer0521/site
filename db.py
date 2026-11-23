"""Database connection and abstraction layer supporting both SQLite and PostgreSQL (Supabase)."""
import os
import re
import sqlite3

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class DictRow(dict):
    """Row wrapper that allows dict access row['col'], index access row[0], and row.keys()."""
    def __init__(self, d, keys=None, values=None):
        super().__init__(d)
        self._keys = list(keys) if keys else list(d.keys())
        self._values = list(values) if values else list(d.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self):
        return self._keys


class DBAdapter:
    """Unified wrapper around sqlite3 or psycopg2 connection."""
    def __init__(self, conn, is_pg=False):
        self._conn = conn
        self.is_pg = is_pg

    def _convert_query(self, query):
        if not self.is_pg:
            return query
        
        q = query
        # Convert INSERT OR IGNORE INTO to INSERT INTO ... ON CONFLICT DO NOTHING
        if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", q, re.IGNORECASE):
            q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", q, flags=re.IGNORECASE)
            q = q.rstrip().rstrip(";")
            q = q + " ON CONFLICT DO NOTHING"

        # If INSERT INTO students without RETURNING, append RETURNING id
        if re.search(r"INSERT\s+INTO\s+students\s*\(", q, re.IGNORECASE) and "RETURNING" not in q.upper():
            q = q.rstrip().rstrip(";") + " RETURNING id"

        # Convert placeholders ? to %s
        q = q.replace("?", "%s")
        return q

    def execute(self, query, params=None):
        if not self.is_pg:
            if params is None:
                return self._conn.execute(query)
            return self._conn.execute(query, params)

        converted = self._convert_query(query)
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        if params is None:
            cur.execute(converted)
        else:
            cur.execute(converted, params)
        return CursorWrapper(cur)

    def executescript(self, script):
        if not self.is_pg:
            return self._conn.executescript(script)
        
        cur = self._conn.cursor()
        cur.execute(script)
        self._conn.commit()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


class CursorWrapper:
    """Unified cursor wrapper matching sqlite3.Cursor interface."""
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None
        # If this was an INSERT with RETURNING id, capture lastrowid
        try:
            if cur.description and len(cur.description) > 0 and cur.description[0][0] == "id":
                row = cur.fetchone()
                if row:
                    self.lastrowid = row["id"]
        except Exception:
            pass

    def fetchone(self):
        try:
            row = self._cur.fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return DictRow(row, keys=[d[0] for d in self._cur.description], values=list(row.values()))

    def fetchall(self):
        try:
            rows = self._cur.fetchall()
        except Exception:
            return []
        if not rows:
            return []
        keys = [d[0] for d in self._cur.description] if self._cur.description else None
        return [DictRow(r, keys=keys, values=list(r.values())) for r in rows]

    def __iter__(self):
        return iter(self.fetchall())


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
  id SERIAL PRIMARY KEY, ref TEXT UNIQUE, name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL, program TEXT NOT NULL, campus TEXT NOT NULL,
  year_level INTEGER NOT NULL DEFAULT 1, consent INTEGER NOT NULL, created TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS documents(
  id SERIAL PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  doc_type TEXT NOT NULL, original TEXT, stored TEXT, status TEXT NOT NULL DEFAULT 'submitted',
  note TEXT DEFAULT '', updated TEXT NOT NULL, UNIQUE(student_id, doc_type));

CREATE TABLE IF NOT EXISTS audit(
  id SERIAL PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS verifications(
  id SERIAL PRIMARY KEY, ts TEXT NOT NULL, ref TEXT, ok INTEGER NOT NULL, ms REAL NOT NULL);

CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS subjects(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  units INTEGER NOT NULL, program TEXT NOT NULL, year_level INTEGER NOT NULL, semester TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sections(
  id SERIAL PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  section_code TEXT NOT NULL, days TEXT NOT NULL, time_start TEXT NOT NULL, time_end TEXT NOT NULL,
  room TEXT NOT NULL, capacity INTEGER NOT NULL, UNIQUE(subject_id, section_code));

CREATE TABLE IF NOT EXISTS enrollments(
  id SERIAL PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE, UNIQUE(student_id, section_id));

CREATE TABLE IF NOT EXISTS advising(
  student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE, status TEXT NOT NULL,
  note TEXT DEFAULT '', updated TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fees(id SERIAL PRIMARY KEY, name TEXT NOT NULL, amount REAL NOT NULL);

CREATE TABLE IF NOT EXISTS payment_plans(
  student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE, plan TEXT NOT NULL,
  method TEXT NOT NULL, chosen TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS payments(
  id SERIAL PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  amount REAL NOT NULL, method TEXT NOT NULL, note TEXT DEFAULT '', ts TEXT NOT NULL);
"""


def get_db_connection(config=None):
    """Returns a DBAdapter connected to either Supabase PostgreSQL or local SQLite."""
    # When TESTING is set, always use local SQLite database configured in config
    if config and config.get("TESTING"):
        db_path = config.get("DATABASE", "uers.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return DBAdapter(conn, is_pg=False)

    db_url = None
    if config and "DATABASE_URL" in config:
        db_url = config["DATABASE_URL"]
    if not db_url:
        db_url = os.environ.get("DATABASE_URL")

    # If DATABASE_URL is PostgreSQL and psycopg2 is available
    if db_url and db_url.startswith("postgres"):
        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2-binary is required for PostgreSQL connection.")
        conn = psycopg2.connect(db_url)
        return DBAdapter(conn, is_pg=True)

    # SQLite fallback
    db_path = config.get("DATABASE", "uers.db") if config else os.environ.get("DATABASE", "uers.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return DBAdapter(conn, is_pg=False)
