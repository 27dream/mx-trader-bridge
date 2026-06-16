"""盘中盯盘 v2：4档卖出触发 + 4级挂单价 + 撤改重挂"""
import os, json, time
from datetime import datetime, date
from dotenv import load_dotenv
load_dotenv()

import trader, db, notifier
from morning_trade import get_realtime_price

# db 没有 _conn，monitor v2 旧代码用 db._conn() — 兼容
if not hasattr(db, '_conn'):
    db._conn = db.get_conn

# ============ 卖出参数 ============
import params as P

# v3：常量从 params.SELL 读取，可被 reflector 调参
def _hard_stop():     return P.SELL['hard_stop_pct']
def _take_profit():   return P.SELL['take_profit_pct']
def _trail_trigger(): return P.SELL['trail_peak_trigger']
def _trail_drawdown():return P.SELL['trail_drawdown']
def _force_sell_hm(): return P.SELL['force_sell_time']  # "14:50"
def _max_hold():      return P.SELL['max_hold_days']
HOLD_DAYS_LIMIT    = 3       # 持仓 3 日未盈利强平
MAX_RETRY_PER_STOCK = 4      # 单票最多重挂 4 次

# ============ 集合竞价时段（禁单）============
def in_call_auction(now_hm: str) -> bool:
    return ('09:20' <= now_hm <= '09:25') or ('14:57' <= now_hm <= '15:00')

# ============ 持仓状态表（追踪 high_since_buy / hold_days）============
def _ensure_state_table():
    with db._conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS position_state (
                code TEXT PRIMARY KEY,
                buy_date TEXT,
                buy_price REAL,
                high_since_buy REAL DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                last_retry_ts TEXT,
                paused INTEGER DEFAULT 0
            )
        """)

def _get_state(code: str) -> dict:
    _ensure_state_table()
    with db._conn() as c:
        r = c.execute("SELECT * FROM position_state WHERE code=?", (code,)).fetchone()
        return dict(r) if r else {}

def _upsert_state(code: str, **fields):
    _ensure_state_table()
    keys = ','.join(fields.keys())
    qs = ','.join(['?'] * len(fields))
    sets = ','.join([f"{k}=excluded.{k}" for k in fields.keys()])
    with db._conn() as c:
        c.execute(f"INSERT INTO position_state(code,{keys}) VALUES(?,{qs}) "
                  f"ON CONFLICT(code) DO UPDATE SET {sets}",
                  (code, *fields.values()))

def _hold_days(buy_date: str) -> int:
    if not buy_date: return 0
    try:
        bd = datetime.strptime(buy_date, '%Y-%m-%d').date()
        return (date.today() - bd).days
    except Exception:
        return 0

# ============ 4档卖出价格策略 ============
def _sell_price_by_retry(code: str, retry: int, fallback: float) -> float | None:
    """
    第1次：bid1（买一价，立即成交）
    第2次：bid1 - 1 tick（再降一档）
    第3次：现价
    第4次：跌停价（≈ -10%）
    """
    rt = get_realtime_price(code) or {}
    bid1 = rt.get('bid1') or rt.get('buy1') or fallback
    last = rt.get('price') or fallback
    limit_down = round(fallback * 0.9, 2)  # 粗估跌停
    if retry == 0: return bid1
    if retry == 1: return round(bid1 - 0.01, 2)
    if retry == 2: return last
    if retry == 3: return limit_down
    return None  # 超过 4 次

# ============ 卖出触发判定（4 档）============
def _decide_exit(p: dict, state: dict, force_exit: bool) -> str | None:
    cost = p.get('_costPrice') or (p['costPrice'] / (10 ** p.get('costPriceDec', 2)))
    price = p.get('_price') or (p['price'] / (10 ** p.get('priceDec', 2)))
    if cost <= 0 or price <= 0: return None
    pnl_pct = (price - cost) / cost

    # 更新历史最高盈亏
    high = max(state.get('high_since_buy', 0) or 0, pnl_pct)
    _upsert_state(p['secCode'], high_since_buy=high)

    # 1) 硬止损
    if pnl_pct <= _hard_stop():
        return f'hard_stop ({pnl_pct*100:+.2f}%)'
    # 2) 止盈
    if pnl_pct >= _take_profit():
        return f'take_profit ({pnl_pct*100:+.2f}%)'
    # 3) 移动止损
    if high >= _trail_trigger() and (high - pnl_pct) >= _trail_drawdown():
        return f'trail_stop (peak={high*100:+.2f}% now={pnl_pct*100:+.2f}%)'
    # 4) 时间止损
    days = _hold_days(state.get('buy_date', ''))
    if days >= HOLD_DAYS_LIMIT and pnl_pct < 0:
        return f'time_stop ({days}d, {pnl_pct*100:+.2f}%)'
    # 5) 收盘前强平（保留兜底）
    if force_exit:
        return f'force_exit_eod'
    return None

# ============ 主入口 ============
def check_and_exit():
    today = datetime.now().strftime('%Y-%m-%d')
    now_hm = datetime.now().strftime('%H:%M')
    print(f"\n[{now_hm}] 🔍 monitor v2 扫描...")

    if in_call_auction(now_hm):
        print(f"⏸️ 集合竞价时段（{now_hm}），禁止下单")
        return

    dec = db.get_today_decision(today)
    risk = json.loads(dec['risk_json']) if (dec and dec.get('risk_json')) else {}
    force_exit_time = risk.get('force_exit_time', '14:50')
    is_force_exit = now_hm >= force_exit_time

    pos = trader.get_positions()
    active = [p for p in pos if p.get('count', 0) > 0 and p.get('availCount', 0) > 0]
    if not active:
        print("无可卖持仓"); return

    for p in active:
        code, name = p['secCode'], p['secName']
        state = _get_state(code)
        if state.get('paused'):
            print(f"  ⏸️ {code} {name} 已暂停（重挂超限）"); continue

        cost = p.get('_costPrice') or (p['costPrice'] / (10 ** p.get('costPriceDec', 2)))
        price = p.get('_price') or (p['price'] / (10 ** p.get('priceDec', 2)))
        qty = p['availCount']
        reason = _decide_exit(p, state, is_force_exit)
        pnl_pct = (price - cost) / cost if cost > 0 else 0

        print(f"  {code} {name} 成本{cost:.2f} 现价{price:.2f} {pnl_pct*100:+.2f}% "
              f"peak={state.get('high_since_buy', 0)*100:+.2f}% "
              f"days={_hold_days(state.get('buy_date',''))} | {reason or '持有'}")

        if not reason:
            continue

        retry = state.get('retry_count', 0) or 0
        if retry >= MAX_RETRY_PER_STOCK:
            _upsert_state(code, paused=1)
            notifier.alert(f"🚫 {code} {name} 重挂{retry}次仍未成交，暂停",
                           level='warn', title='卖单超限')
            continue

        sell_px = _sell_price_by_retry(code, retry, price)
        if sell_px is None:
            _upsert_state(code, paused=1); continue

        print(f"  → 第{retry+1}次挂卖：¥{sell_px:.2f} × {qty}（{reason}）")
        try:
            r = trader.sell_safe(code, qty, price=sell_px)
            order_id = trader._extract_order_id(r.get('order_resp') or {})
            fill = r.get('fill_info', {})
            fill_price = fill.get('avgPrice', sell_px) or sell_px
            status = 'filled' if r.get('ok') else 'submit_only'
            db.log_trade(today, code, name, 'SELL', fill_price, qty, order_id,
                         status, f"{reason}/retry{retry+1}", dec['id'] if dec else None, r)
            db.log_signal(today, code, reason.split()[0], f"{name} {reason}")

            if r.get('ok'):
                print(f"  ✂️ 已成交 ¥{fill_price:.2f}")
                notifier.notify_fill(code, name, 'SELL', fill_price, qty, reason)
                # 清空状态
                with db._conn() as c:
                    c.execute("DELETE FROM position_state WHERE code=?", (code,))
            else:
                _upsert_state(code, retry_count=retry+1,
                              last_retry_ts=datetime.now().isoformat(timespec='seconds'))
                print(f"  ⚠️ 未成交，下次重挂（已 {retry+1}/{MAX_RETRY_PER_STOCK}）")
        except trader.TradeError as e:
            print(f"  ❌ 卖单被拒: {e}")
            db.log_trade(today, code, name, 'SELL', sell_px, qty, '', 'rejected',
                         f"{reason}/retry{retry+1}/rejected", dec['id'] if dec else None,
                         {'error': str(e)})
            notifier.notify_reject(code, name, f"卖单被拒({reason}): {str(e)[:150]}")
            _upsert_state(code, retry_count=retry+1,
                          last_retry_ts=datetime.now().isoformat(timespec='seconds'))
        time.sleep(1)

if __name__ == '__main__':
    check_and_exit()
