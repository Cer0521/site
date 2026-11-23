import os
import psycopg2
from dotenv import load_dotenv
from db import DBAdapter, PG_SCHEMA

load_dotenv()
db_url = os.getenv("DATABASE_URL")
print(f"Connecting to: {db_url.split('@')[1] if '@' in db_url else db_url}")

conn = psycopg2.connect(db_url)
adapter = DBAdapter(conn, is_pg=True)
adapter.executescript(PG_SCHEMA)

cur = conn.cursor()
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;")
tables = [r[0] for r in cur.fetchall()]
print("SUCCESS: Tables in Supabase:", tables)
conn.close()
