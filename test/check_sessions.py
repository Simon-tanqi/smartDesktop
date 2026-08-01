import sqlite3
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sids = ['2026073112_5', '2026073112_6', '2026073112_7', '2026080109_1']
for sid in sids:
    db = 'C:\\Users\\username\\AppData\\Roaming\\MyAgent\\memory\\short_term\\' + sid + '\\messages.db'
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute('SELECT role, substr(content, 1, 80) FROM chat_history ORDER BY id LIMIT 3')
    rows = c.fetchall()
    print(f'=== {sid} ===')
    for r in rows:
        print(f'  {r[0]}: {r[1]}')
    c.execute('SELECT COUNT(*) FROM chat_history')
    total = c.fetchone()[0]
    print(f'  total: {total} rows')
    # Look at last 2 messages
    c.execute('SELECT role, substr(content, 1, 100) FROM chat_history ORDER BY id DESC LIMIT 2')
    for r in c.fetchall():
        print(f'  last: {r[0]}: {r[1]}')
    print()
    conn.close()
