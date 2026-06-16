"""统一持仓/资产/订单查询入口（hermes 和所有外部脚本统一走这里）

CLI:
  python query.py snapshot      # 默认：账户 + 持仓 + 今日订单
  python query.py balance
  python query.py positions
  python query.py orders [status=0]
  python query.py trades [date=today]   # bridge 本地 trades 表
"""
import os, json, sys, sqlite3
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
import trader

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db.sqlite')


def snapshot() -> dict:
    """一次性返回账户全貌（hermes 推荐入口）"""
    bal = trader.get_balance()
    pos = trader.get_positions()
    orders = trader.get_orders(0)
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
    today_orders = [o for o in orders if (o.get('time') or 0) >= today_start]

    # 整理持仓
    norm_pos = []
    for p in pos:
        if p.get('count', 0) <= 0:
            continue
        cost = p.get('_costPrice') or (p['costPrice'] / (10 ** p.get('costPriceDec', 2)))
        price = p.get('_price') or (p['price'] / (10 ** p.get('priceDec', 2)))
        mkt_val = price * p['count']
        pnl = (price - cost) * p['count']
        pnl_pct = (price - cost) / cost if cost > 0 else 0
        norm_pos.append({
            'code': p['secCode'], 'name': p['secName'],
            'qty': p['count'], 'avail': p.get('availCount', 0),
            'cost': round(cost, 4), 'price': round(price, 4),
            'mkt_val': round(mkt_val, 2),
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct * 100, 2),
        })

    # 整理订单（妙想字段名归一化）
    norm_orders = []
    for o in today_orders:
        pdec = o.get('priceDec', 2)
        norm_orders.append({
            'order_id': o.get('id'),
            'code': o.get('secCode'), 'name': o.get('secName'),
            'side': 'BUY' if o.get('drt') == 1 else 'SELL',
            'price': (o.get('price') or 0) / (10 ** pdec),
            'qty': o.get('count', 0),
            'trade_qty': o.get('tradeCount', 0),
            'trade_price': (o.get('tradePrice') or 0) / (10 ** pdec) if o.get('tradeCount') else 0,
            'status': o.get('status'),  # 4=已成 2=已报 8=已撤
            'time': datetime.fromtimestamp(o.get('time', 0)).strftime('%H:%M:%S'),
        })

    bd = bal.get('data', {}) if bal.get('code') == '200' else {}
    return {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'account': {
            'name': bd.get('accName'),
            'total_assets': bd.get('totalAssets', 0),
            'avail_balance': bd.get('availBalance', 0),
            'total_pos_value': bd.get('totalPosValue', 0),
            'total_pos_pct': bd.get('totalPosPct', 0),
            'frozen_money': bd.get('frozenMoney', 0),
            'nav': bd.get('nav', 0),
        },
        'positions': norm_pos,
        'today_orders': norm_orders,
    }


def local_trades(date: str = None) -> list:
    """查 bridge 本地账本"""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE date=? ORDER BY id DESC", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def format_text(snap: dict) -> str:
    """人类友好的中文文本"""
    a = snap['account']
    lines = [f"📊 妙想模拟盘 [{snap['time']}]  账号: {a['name']}",
             f"💰 总资产 {a['total_assets']:,.2f} | 可用 {a['avail_balance']:,.2f} | "
             f"持仓 {a['total_pos_value']:,.2f} ({a['total_pos_pct']:.1f}%) | "
             f"净值 {a['nav']:.4f}"]
    if snap['positions']:
        lines.append("\n📦 持仓：")
        for p in snap['positions']:
            arrow = '🟢' if p['pnl'] >= 0 else '🔴'
            lines.append(f"  {arrow} {p['code']} {p['name']} ×{p['qty']} | "
                         f"成本{p['cost']:.2f} 现价{p['price']:.2f} | "
                         f"市值¥{p['mkt_val']:,.0f} | "
                         f"浮盈{p['pnl']:+,.0f} ({p['pnl_pct']:+.2f}%)")
    else:
        lines.append("📦 持仓：空")

    if snap['today_orders']:
        lines.append("\n📋 今日订单：")
        st_map = {2: '已报', 4: '已成', 8: '已撤'}
        for o in snap['today_orders']:
            st = st_map.get(o['status'], f"st={o['status']}")
            tp = f" 成交{o['trade_qty']}@{o['trade_price']:.2f}" if o['trade_qty'] else ""
            lines.append(f"  [{o['time']}] {o['side']} {o['code']} {o['name']} "
                         f"×{o['qty']}@{o['price']:.2f} → {st}{tp}")
    return '\n'.join(lines)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'snapshot'
    if cmd == 'snapshot':
        snap = snapshot()
        if '--json' in sys.argv:
            print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_text(snap))
    elif cmd == 'balance':
        print(json.dumps(trader.get_balance(), ensure_ascii=False, indent=2))
    elif cmd == 'positions':
        print(json.dumps(trader.get_positions(), ensure_ascii=False, indent=2, default=str))
    elif cmd == 'orders':
        st = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        print(json.dumps(trader.get_orders(st), ensure_ascii=False, indent=2))
    elif cmd == 'trades':
        date = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(local_trades(date), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
