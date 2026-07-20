import sqlite3

DATABASE_NAME = "falcon.db"

connection = sqlite3.connect(DATABASE_NAME)

cursor = connection.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS scans (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    token TEXT,

    chain_name TEXT,

    score INTEGER,

    liquidity REAL,

    market_cap REAL,

    volume5 REAL,

    volume1h REAL,

    buys INTEGER,

    sells INTEGER,

    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP

)
""")

connection.commit()

connection.close()

print("🦅 Falcon Database Created Successfully!")