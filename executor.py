"""执行器 — 买入挂单 + 撤改重挂逻辑

买入流程：
  T+0:00 9:30 开盘 → 不立刻下单（等方向）
  T+0:05 9:35 → 候选股若开盘 ≤ target×1.005，限价挂 target_buy_price
  T+0:10 9:40 → 未成交 → 撤单改 ask1 重挂
  T+0:15 9:45 → 仍未成 → 撤单放弃（避免追涨）
  T+0:30 10:00 → 买入决策结束

每只候选股独立状态机，记录在 db.actions 表
"""
import os, json, time, sqlite3
from datetime import datetime
from typing import Dict, Optional
import requests
from config_store import load as _load_cfg
import trader
from risk_guard import check_buy as _risk_check_buy
from db import get_conn as _conn
import params as P

_cfg = _load_cfg()
MX_APIKEY = _cfg.get('mx_apikey', '') or os.getenv('MX_APIKEY', '')

CANDIDATES_FILE = '/home/ubuntu/projects/mx-trader-bridge/candidates.json'
EM_QUOTE = 'https://push2delay.eastmoney.com/api/qt/stock/get'


def _quote(code: str) -> Dict:
    """获取实时盘口"""
    secid = ('1.' if code.startswith(('6', '5')) else '0.') + code
    try:
        r = requests.get(
            EM_QUOTE,
            params={'secid': secid,
                    'fields': 'f43,f44,f45,f46,f47,f48,f31,f32,f33,f34,f51,f52,f60,f86'},
            timeout=5
        )
        d = r.json().get('data', {})
        return {
            'last': (d.get('f43') or 0) / 100,
            'open': (d.get('f46') or 0) / 100,
            'high': (d.get('f44') or 0) / 100,
            'low': (d.get('f45') or 0) / 100,
            'pre_close': (d.get('f60') or 0) / 100,
            'bid1': (d.get('f31') or 0) / 100,
            'ask1': (d.get('f51') or 0) / 100,
            'volume': d.get('f47') or 0,
        }
    except Exception as e:
        print(f'[executor] quote {code} error: {e}')
        return {}


def _action_log(code: str, name: str, action: str, price: float,
                qty: int, status: str, reason: str, order_id: str = ''):
    """记录决策动作到 actions 表"""
    conn = _conn()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS actions ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, code TEXT, name TEXT, '
        'action TEXT, price REAL, qty INTEGER, status TEXT, reason TEXT, order_id TEXT)'
    )
    conn.execute(
        'INSERT INTO actions (ts, code, name, action, price, qty, status, reason, order_id) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (datetime.now().isoformat(timespec='seconds'), code, name, action,
         price, qty, status, reason, order_id)
    )
    conn.commit()
    conn.close()


def _calc_qty(price: float, total_assets: float, pos_pct_limit: float) -> int:
    """按仓位百分比计算可买股数（向下取整到100股）"""
    if price <= 0:
        return 0
    budget = total_assets * pos_pct_limit
    raw = int(budget / price)
    return (raw // 100) * 100


def _last_actions_for_code(code: str, action_prefix: str = 'BUY') -> list:
    """查询某只股今天的所有 BUY 动作"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, ts, action, price, qty, status, order_id FROM actions '
        'WHERE code=? AND action LIKE ? AND ts LIKE ? ORDER BY id',
        (code, action_prefix + '%', today + '%')
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _execute_buy_for_pick(pick: Dict, total_assets: float) -> Dict:
    """单只候选股买入状态机"""
    code = pick['code']
    name = pick['name']
    target = pick['target_buy_price']
    _max_pos = P.TRADING['max_position_pct']
    pos_pct = min(pick.get('pos_pct', _max_pos), _max_pos)  # 单票 ≤ max_position_pct

    history = _last_actions_for_code(code, 'BUY')
    attempt = len(history)

    # 已有 4 次尝试 → 放弃
    if attempt >= 4:
        return {'code': code, 'status': 'skip', 'reason': '已尝试4次'}

    # 已经成交过 → 跳过
    for h in history:
        if h[5] == 'filled':
            return {'code': code, 'status': 'skip', 'reason': '已持仓'}

    q = _quote(code)
    if not q.get('last'):
        return {'code': code, 'status': 'fail', 'reason': '无盘口'}

    last, ask1 = q['last'], q['ask1']

    # 风控预检
    risk = _risk_check_buy(code, last, qty=0)
    if not risk.get('ok', True):
        _action_log(code, name, 'BUY_BLOCKED', last, 0, 'blocked',
                    f'风控:{risk.get("reason", "")}')
        return {'code': code, 'status': 'blocked', 'reason': risk.get('reason', '')}

    # 第 N 次尝试的挂单价
    if attempt == 0:
        # 第一次：开盘 ≤ target×1.005 → 挂 target
        if q['open'] > target * 1.005:
            return {'code': code, 'status': 'skip',
                    'reason': f'开盘{q["open"]}超过目标{target*1.005:.2f}'}
        order_price = target
    elif attempt == 1:
        # 第二次：撤单改 ask1
        order_price = ask1 if ask1 > 0 else last
    elif attempt == 2:
        # 第三次：现价 + 0.5%
        order_price = round(last * 1.005, 2)
    else:
        # 第四次：直接拉到涨停价附近（市价单）
        pre_close = q.get('pre_close', last)
        order_price = round(pre_close * 1.099, 2)

    qty = _calc_qty(order_price, total_assets, pos_pct)
    if qty < 100:
        return {'code': code, 'status': 'fail', 'reason': f'数量不足100股 (qty={qty})'}

    # 下单
    try:
        res = trader.buy(code, qty, order_price)
        # 解析订单 ID
        order_id = (res.get('data', {}) or {}).get('orderID', '') or \
                   (res.get('data', {}) or {}).get('id', '')
        _action_log(code, name, f'BUY_ATTEMPT{attempt+1}', order_price, qty,
                    'submitted', pick.get('reason', ''), str(order_id))
        return {'code': code, 'status': 'submitted', 'price': order_price,
                'qty': qty, 'order_id': order_id, 'attempt': attempt + 1}
    except Exception as e:
        _action_log(code, name, f'BUY_ATTEMPT{attempt+1}', order_price, qty,
                    'error', str(e)[:200])
        return {'code': code, 'status': 'error', 'reason': str(e)[:200]}


def execute_buys() -> list:
    """主入口：读取 candidates，执行买入"""
    if not os.path.exists(CANDIDATES_FILE):
        print('[executor] 无候选文件，跳过')
        return []

    with open(CANDIDATES_FILE, encoding='utf-8') as f:
        cand = json.load(f)

    today = datetime.now().strftime('%Y-%m-%d')
    if cand.get('date') != today:
        print(f'[executor] 候选不是今日({cand.get("date")})，跳过')
        return []

    bal = trader.get_balance().get('data', {})
    total_assets = (bal.get('totalAssets') or 0) / (bal.get('currencyUnit') or 1000)
    avail = (bal.get('availBalance') or 0) / (bal.get('currencyUnit') or 1000)
    print(f'[executor] 资产 {total_assets:.0f} 可用 {avail:.0f}')

    results = []
    for pick in cand.get('picks', []):
        r = _execute_buy_for_pick(pick, total_assets)
        results.append(r)
        print(f'  [{pick["strategy"]}] {pick["code"]} {pick["name"]} → {r}')
        time.sleep(1)  # 避免限流
    return results


def cancel_unfilled_after(minutes: int = 15) -> int:
    """超时撤单：超过 N 分钟未成交的 BUY 单全撤"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT order_id, code FROM actions WHERE action LIKE "BUY_%" '
        'AND status="submitted" AND ts LIKE ?', (today + '%',)
    )
    rows = cur.fetchall()
    conn.close()

    cancelled = 0
    for order_id, code in rows:
        if not order_id:
            continue
        try:
            trader.cancel_order(order_id, code)
            _action_log(code, '', 'BUY_CANCEL', 0, 0, 'cancelled',
                        f'超时{minutes}min撤单', str(order_id))
            cancelled += 1
        except Exception as e:
            print(f'[executor] cancel {order_id} error: {e}')
    print(f'[executor] 撤单 {cancelled} 笔')
    return cancelled


if __name__ == '__main__':
    execute_buys()
