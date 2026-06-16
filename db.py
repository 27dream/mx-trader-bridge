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
    -- v2 新增：候选池（每日 picker 输出）
    CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        sec_code TEXT NOT NULL,
        sec_name TEXT,
        strategy TEXT,                   -- A_pullback / B_first_limit / C_main_inflow
        confidence REAL,                 -- 0-1
        target_buy_price REAL,
        sl_pct REAL,                     -- 止损% (负数)
        tp_pct REAL,                     -- 止盈% (正数)
        max_position_pct REAL,           -- 单票最大仓位占总资产
        reason TEXT,                     -- LLM 选股理由
        risk_note TEXT,                  -- LLM 风险提示
        approved INTEGER DEFAULT 1,      -- 1通过/0驳回
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- v2 新增：每个挂单的生命周期
    CREATE TABLE IF NOT EXISTS order_lifecycle (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        sec_code TEXT NOT NULL,
        sec_name TEXT,
        action TEXT NOT NULL,            -- BUY / SELL
        attempt INTEGER DEFAULT 1,       -- 第几次重挂
        price REAL,
        quantity INTEGER,
        order_id TEXT,
        status TEXT,                     -- submitted / filled / cancelled / failed / timeout
        trigger_reason TEXT,             -- buy_strategy_A / stop_loss / take_profit / time_exit / move_stop
        candidate_id INTEGER,
        position_cost REAL,              -- 卖出时记录建仓成本
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- v2 新增：周报 + 参数迭代建议
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period TEXT NOT NULL,            -- daily / weekly
        period_start TEXT,
        period_end TEXT,
        total_trades INTEGER,
        win_trades INTEGER,
        loss_trades INTEGER,
        win_rate REAL,
        total_pnl REAL,
        avg_win_pct REAL,
        avg_loss_pct REAL,
        worst_trade TEXT,                -- json
        best_trade TEXT,                  -- json
        strategy_perf TEXT,               -- json: 各策略胜率
        param_patch TEXT,                 -- json: LLM 建议的参数修改
        ai_reasoning TEXT,
        applied INTEGER DEFAULT 0,        -- 0未采纳/1已采纳
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- v2 新增：状态机决策动作（executor 写入）
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT,
        name TEXT,
        action TEXT,                       -- BUY_1/BUY_2/BUY_3/BUY_4/SELL_1.../CANCEL/RETRY
        price REAL,
        qty INTEGER,
        status TEXT,                       -- submitted/filled/partial/cancelled/rejected/timeout
        reason TEXT,
        order_id TEXT,
        retry_count INTEGER DEFAULT 0,
        strategy TEXT,                     -- A/B/C
        decision_id INTEGER,
        raw_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_actions_code_ts ON actions(code, ts);
    CREATE INDEX IF NOT EXISTS idx_actions_action ON actions(action);
    -- v2 新增：持仓状态（buy_date/high_since_buy/retry/paused）
    CREATE TABLE IF NOT EXISTS position_state (
        code TEXT PRIMARY KEY,
        buy_date TEXT,
        buy_price REAL,
        high_since_buy REAL DEFAULT 0,
        retry_count INTEGER DEFAULT 0,
        last_retry_ts TEXT,
        paused INTEGER DEFAULT 0,
        strategy TEXT,                     -- A/B/C
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    -- v3 新增：参数变更历史（自动调参 + 一键回滚）
    CREATE TABLE IF NOT EXISTS params_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        source TEXT,                        -- reflector_weekly / manual / rollback
        scope TEXT,                         -- STRATEGY_A / STRATEGY_B / ...
        param_key TEXT,
        old_value TEXT,
        new_value TEXT,
        change_pct REAL,                    -- 相对变化幅度
        reason TEXT,                        -- LLM/规则给出的修改理由
        rolled_back INTEGER DEFAULT 0,
        rolled_back_at TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_params_history_applied ON params_history(applied_at);
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

def save_candidate(date, sec_code, sec_name, strategy, confidence,
                   target_buy_price, sl_pct, tp_pct, max_position_pct,
                   reason, risk_note, approved=1):
    c = get_conn()
    cur = c.execute(
        """INSERT INTO candidates(date,sec_code,sec_name,strategy,confidence,
            target_buy_price,sl_pct,tp_pct,max_position_pct,reason,risk_note,approved)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, sec_code, sec_name, strategy, confidence,
         target_buy_price, sl_pct, tp_pct, max_position_pct,
         reason, risk_note, approved))
    cid = cur.lastrowid
    c.commit(); c.close()
    return cid

def get_candidates(date, approved_only=True):
    c = get_conn()
    sql = "SELECT * FROM candidates WHERE date=?"
    if approved_only:
        sql += " AND approved=1"
    sql += " ORDER BY confidence DESC"
    rows = c.execute(sql, (date,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def log_lifecycle(date, sec_code, sec_name, action, attempt, price, quantity,
                  order_id, status, trigger_reason, candidate_id=None, position_cost=None):
    c = get_conn()
    cur = c.execute(
        """INSERT INTO order_lifecycle(date,sec_code,sec_name,action,attempt,price,
            quantity,order_id,status,trigger_reason,candidate_id,position_cost)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, sec_code, sec_name, action, attempt, price, quantity,
         order_id, status, trigger_reason, candidate_id, position_cost))
    lid = cur.lastrowid
    c.commit(); c.close()
    return lid

def update_lifecycle_status(lifecycle_id, status):
    c = get_conn()
    c.execute("UPDATE order_lifecycle SET status=? WHERE id=?", (status, lifecycle_id))
    c.commit(); c.close()

def get_open_buy_attempts(date, sec_code):
    """查某只股票当日已重挂次数"""
    c = get_conn()
    r = c.execute(
        """SELECT COUNT(*) as n FROM order_lifecycle
           WHERE date=? AND sec_code=? AND action='BUY'""",
        (date, sec_code)).fetchone()
    c.close()
    return r['n'] if r else 0

def get_pending_orders_today(date):
    """查今天还在 submitted 状态、未成交的挂单"""
    c = get_conn()
    rows = c.execute(
        """SELECT * FROM order_lifecycle
           WHERE date=? AND status='submitted'
           ORDER BY created_at""",
        (date,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def save_review(period, period_start, period_end, total_trades, win, loss, win_rate,
                total_pnl, avg_win_pct, avg_loss_pct, worst_trade, best_trade,
                strategy_perf, param_patch, ai_reasoning):
    import json
    c = get_conn()
    cur = c.execute(
        """INSERT INTO reviews(period,period_start,period_end,total_trades,win_trades,
            loss_trades,win_rate,total_pnl,avg_win_pct,avg_loss_pct,worst_trade,best_trade,
            strategy_perf,param_patch,ai_reasoning)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (period, period_start, period_end, total_trades, win, loss, win_rate,
         total_pnl, avg_win_pct, avg_loss_pct,
         json.dumps(worst_trade, ensure_ascii=False) if worst_trade else None,
         json.dumps(best_trade, ensure_ascii=False) if best_trade else None,
         json.dumps(strategy_perf, ensure_ascii=False) if strategy_perf else None,
         json.dumps(param_patch, ensure_ascii=False) if param_patch else None,
         ai_reasoning))
    rid = cur.lastrowid
    c.commit(); c.close()
    return rid

def get_trades_in_range(start_date, end_date):
    c = get_conn()
    rows = c.execute(
        "SELECT * FROM trades WHERE date>=? AND date<=? ORDER BY date,id",
        (start_date, end_date)).fetchall()
    c.close()
    return [dict(r) for r in rows]

if __name__ == '__main__':
    init_db()
    print("✅ DB initialized at", DB_PATH)
