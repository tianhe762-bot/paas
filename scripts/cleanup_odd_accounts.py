import sqlite3

conn = sqlite3.connect("/app/data/account.db")
rows = conn.execute(
    "SELECT id, name FROM accounts WHERE namespace='default' "
    "AND name NOT IN ('\u5fae\u4fe1', '\u652f\u4ed8\u5b9d', "
    "'\u94f6\u884c\u5361', '\u73b0\u91d1', '\u4fe1\u7528\u5361')"
).fetchall()
print("odd accounts:", rows)
for r in rows:
    conn.execute("DELETE FROM accounts WHERE id=?", (r[0],))
conn.commit()
conn.close()

