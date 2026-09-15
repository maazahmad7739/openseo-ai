import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import env
env.load_env_file()
from agents.seo_agent import run_agent
import db as database

conn = database.get_connection()
try:
    with conn.cursor() as cur:
        cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
        row = cur.fetchone()
    if not row:
        print("no site; run sync first")
        sys.exit(1)
    conn.rollback()
    summary = run_agent(row[0], conn)
    print(json.dumps(summary, default=str, indent=2))
finally:
    conn.close()