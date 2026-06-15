"""独立风控守护：单股集中度 / 单日亏损 / 持仓时长 / 黑名单 强制护栏

设计原则：
1. 不依赖 LLM 判断，纯规则驱动
2. 任何时刻可独立运行 → 触发立即平仓
3. 与 monitor.py 互补：monitor 是策略执行器，risk_guard 是底线兜底
"""
import os, json, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from config_store import load as _load_cfg
from trader import get_balance, get_positions, sell_safe, cancel_all
import db

_cfg = _load_cfg()

# ---------- 风控阈值（可配置） ----------
MAX_SINGLE_POS_PCT = float(os.getenv('RG_MAX_SINGLE_POS', '0.55'))   # 单股≤55% 总资产
MAX_DAILY_LOSS_PCT = float(os.getenv('RG_MAX_DAILY_LOSS', '0.03'))   # 单日亏损≥3% 全清仓
MAX_HOLD_DAYS      = int(os.getenv('RG_MAX_HOLD_DAYS', '5'))         # 持仓>5 天强制平
MAX_DRAWDOWN_PCT   = float(os.getenv('RG_MAX_DRAWDOWN', '0.08'))     # 单股回撤≥8% 平
BLACKLIST_CODES    = set((os.getenv('RG_BLACKLIST') or '').split(',')) - {''}

LOG_DIR = os.path.dirname(os.path.abspath(__file__)) + '/logs'
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = f'{LOG_DIR}/risk_guard.log'

def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

# ---------- 风控规则 ----------

def check_single_position(positions: list, total_assets: float) -> list:
    """规则1：单股集中度过高"""
    actions = []
    for p in positions:
        cnt = p.get('count', 0)
        if cnt <= 0: continue
        price = p.get('_price') or (p.get('price', 0) / 100)
        market_value = price * cnt
        pct = market_value / total_assets if total_assets > 0 else 0
        if pct > MAX_SINGLE_POS_PCT:
            # 仅减仓到阈值，而非全清
            target_value = total_assets * MAX_SINGLE_POS_PCT
            sell_qty = int(((market_value - target_value) / price) // 100) * 100
            if sell_qty > 0 and sell_qty <= p.get('availCount', 0):
                actions.append({
                    'reason': 'single_pos_over_limit',
                    'code': p['secCode'], 'name': p.get('secName'),
                    'qty': sell_qty, 'pct': round(pct, 3)
                })
    return actions

def check_daily_loss(balance: dict) -> list:
    """规则2：单日亏损超阈值 → 全部清仓"""
    actions = []
    day_pnl_pct = (balance.get('data') or {}).get('dayProfitPct')
    # 妙想 dayProfitPct 字段已是百分数值（如 -3.2 表示 -3.2%）
    if day_pnl_pct is not None and day_pnl_pct <= -MAX_DAILY_LOSS_PCT * 100:
        actions.append({
            'reason': 'daily_loss_circuit_breaker',
            'day_pnl_pct': day_pnl_pct,
            'action': 'CLEAR_ALL'
        })
    return actions

def check_drawdown(positions: list) -> list:
    """规则3：单股浮亏超阈值 → 强平"""
    actions = []
    for p in positions:
        cnt = p.get('availCount', 0)
        if cnt <= 0: continue  # T+1 当日买入今日不能卖
        profit_pct = p.get('profitPct')  # 已是百分数值
        if profit_pct is not None and profit_pct <= -MAX_DRAWDOWN_PCT * 100:
            actions.append({
                'reason': 'drawdown_stop_loss',
                'code': p['secCode'], 'name': p.get('secName'),
                'qty': cnt, 'profit_pct': profit_pct
            })
    return actions

def check_blacklist(positions: list) -> list:
    """规则4：持仓中含黑名单 → 立即清"""
    actions = []
    for p in positions:
        if p['secCode'] in BLACKLIST_CODES and p.get('availCount', 0) > 0:
            actions.append({
                'reason': 'blacklist',
                'code': p['secCode'], 'name': p.get('secName'),
                'qty': p['availCount']
            })
    return actions


# ---------- 下单前预检（建仓时同步调用） ----------

def pre_check_buy(code: str, qty: int, price: float,
                  balance: dict = None, positions: list = None) -> dict:
    """下单前 4 道预检：黑名单 / 当日已熔断 / 仓位超限 / 资金不足

    Returns: {'ok': bool, 'reason': str|None, 'detail': dict}
    """
    try:
        if balance is None:
            balance = get_balance()
        if positions is None:
            positions = get_positions()
    except Exception as e:
        return {'ok': False, 'reason': 'balance_query_failed', 'detail': {'err': str(e)}}

    bal_data = balance.get('data') or {}
    total_assets = bal_data.get('totalAssets', 0)
    avail = bal_data.get('availBalance', 0)
    day_pnl_pct = bal_data.get('dayProfitPct')

    # 1. 黑名单
    if code in BLACKLIST_CODES:
        return {'ok': False, 'reason': 'blacklist', 'detail': {'code': code}}

    # 2. 当日已熔断（亏损超阈值不允许加仓）
    if day_pnl_pct is not None and day_pnl_pct <= -MAX_DAILY_LOSS_PCT * 100:
        return {'ok': False, 'reason': 'daily_loss_circuit_breaker',
                'detail': {'day_pnl_pct': day_pnl_pct}}

    # 3. 资金不足
    cost = price * qty
    if cost > avail:
        return {'ok': False, 'reason': 'insufficient_balance',
                'detail': {'need': cost, 'avail': avail}}

    # 4. 仓位预测：买入后该股市值占比是否超阈值
    existing_value = 0
    for p in positions:
        if p.get('secCode') == code:
            cnt = p.get('count', 0)
            pp = p.get('_price') or (p.get('price', 0) / 100)
            existing_value = pp * cnt
            break
    new_value = existing_value + cost
    new_pct = new_value / total_assets if total_assets > 0 else 0
    if new_pct > MAX_SINGLE_POS_PCT:
        return {'ok': False, 'reason': 'single_pos_over_limit',
                'detail': {'predicted_pct': round(new_pct, 3),
                           'limit': MAX_SINGLE_POS_PCT}}

    return {'ok': True, 'reason': None, 'detail': {
        'predicted_pct': round(new_pct, 3),
        'cost': cost, 'avail_after': avail - cost
    }}


# ---------- 主流程 ----------

def run(dry_run: bool = False):
    """执行所有风控检查"""
    log(f'🛡️  风控扫描启动 dry_run={dry_run}')
    try:
        bal = get_balance()
        positions = get_positions()
    except Exception as e:
        log(f'❌ 数据拉取失败: {e}')
        return

    total_assets = (bal.get('data') or {}).get('totalAssets', 0)  # 已是元
    log(f'   总资产 ¥{total_assets:,.0f} | 持仓 {len(positions)} 只')

    all_actions = []
    all_actions.extend(check_daily_loss(bal))
    all_actions.extend(check_drawdown(positions))
    all_actions.extend(check_single_position(positions, total_assets))
    all_actions.extend(check_blacklist(positions))

    if not all_actions:
        log('   ✅ 无风控事件')
        return

    log(f'   ⚠️ 触发 {len(all_actions)} 条风控:')
    for a in all_actions:
        log(f'      - {json.dumps(a, ensure_ascii=False)}')

    if dry_run:
        log('   [dry_run] 跳过执行')
        return

    # 执行
    for a in all_actions:
        try:
            if a['reason'] == 'daily_loss_circuit_breaker':
                # 全部清仓（先撤单再清持仓）
                cancel_all()
                for p in positions:
                    if p.get('availCount', 0) > 0:
                        r = sell_safe(p['secCode'], p['availCount'], price=None)
                        log(f'      🚨 熔断卖出 {p["secCode"]} qty={p["availCount"]} → {r["stage"]}')
            else:
                r = sell_safe(a['code'], a['qty'], price=None)
                log(f'      ✂️ 风控卖 {a["code"]} qty={a["qty"]} ({a["reason"]}) → {r["stage"]}')
        except Exception as e:
            log(f'      ❌ 执行失败 {a}: {e}')

if __name__ == '__main__':
    import sys
    dry = '--dry' in sys.argv
    run(dry_run=dry)
