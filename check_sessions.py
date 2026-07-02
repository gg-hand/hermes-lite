import sqlite3
import os

os.chdir('data')
conn = sqlite3.connect('sessions.db')
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cursor.fetchall()]
print('Tables:', tables)
for t in tables:
    cur = conn.execute(f'SELECT * FROM {t} LIMIT 1')
    cols = [d[0] for d in cur.description]
    print(f'  {t} columns: {cols}')
    count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t}: {count} rows')
conn.close()
