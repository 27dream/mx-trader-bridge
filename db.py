"""数据库：战绩 + 决策 + 信号日志"""
import sqlite3, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'db.sqlite')

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = get_conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        strategy_name TEXT,
        dsl_json TEXT,
        risk_json TEXT,
        candidates_json TEXT,
        ai_reasoning TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        sec_code TEXT NOT NULL,
        sec_name TEXT,
        action TEXT NOT NULL,           -- BUY / SELL
        price REAL,
        quantity INTEGER,
        order_id TEXT,
        status TEXT,                    -- submitted / filled / failed
        reason TEXT,                    -- 触发原因：strategy / stop_loss / take_profit / time_exit
        decision_id INTEGER,
        raw_response TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        sec_code TEXT,
        signal_type TEXT,
        message TEXT,
        triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS daily_recap (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT UNIQUE NOT NULL,
        total_assets REAL,
        day_profit REAL,
        day_profit_pct REAL,
        win_count INTEGER DEFAULT 0,
        loss_count INTEGER DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    c.commit()
    c.close()

def log_decision(date, strategy_name, dsl, risk, candidates, reasoning):
    import json
    c = get_conn()
    cur = c.execute(
        "INSERT INTO decisions(date,strategy_name,dsl_json,risk_json,candidates_json,ai_reasoning) VALUES(?,?,?,?,?,?)",
        (date, strategy_name, json.dumps(dsl, ensure_ascii=False), json.dumps(risk, ensure_ascii=False),
         json.dumps(candidates, ensure_ascii=False), reasoning))
    did = cur.lastrowid
    c.commit(); c.close()
    return did

def log_trade(date, sec_code, sec_name, action, price, qty, order_id, status, reason, decision_id=None, raw=None):
    import json
    c = get_conn()
    c.execute(
        "INSERT INTO trades(date,sec_code,sec_name,action,price,quantity,order_id,status,reason,decision_id,raw_response) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (date, sec_code, sec_name, action, price, qty, order_id, status, reason, decision_id,
         json.dumps(raw, ensure_ascii=False) if raw else None))
    c.commit(); c.close()

def log_signal(date, sec_code, signal_type, message):
    c = get_conn()
    c.execute("INSERT INTO signals(date,sec_code,signal_type,message) VALUES(?,?,?,?)",
              (date, sec_code, signal_type, message))
    c.commit(); c.close()

def save_recap(date, total_assets, day_profit, day_profit_pct, win, loss, notes):
    c = get_conn()
    c.execute("""INSERT INTO daily_recap(date,total_assets,day_profit,day_profit_pct,win_count,loss_count,notes)
                 VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(date) DO UPDATE SET
                   total_assets=excluded.total_assets,
                   day_profit=excluded.day_profit,
                   day_profit_pct=excluded.day_profit_pct,
                   win_count=excluded.win_count,
                   loss_count=excluded.loss_count,
                   notes=excluded.notes""",
              (date, total_assets, day_profit, day_profit_pct, win, loss, notes))
    c.commit(); c.close()

def get_recent_recaps(days=7):
    c = get_conn()
    rows = c.execute("SELECT * FROM daily_recap ORDER BY date DESC LIMIT ?", (days,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def get_today_decision(date):
    c = get_conn()
    r = c.execute("SELECT * FROM decisions WHERE date=? ORDER BY id DESC LIMIT 1", (date,)).fetchone()
    c.close()
    return dict(r) if r else None

if __name__ == '__main__':
    init_db()
    print("✅ DB initialized at", DB_PATH)
