"""盘中盯盘：止损/止盈/超时强平"""
import os, json, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

import trader, db
from morning_trade import get_realtime_price

def check_and_exit():
    """每5分钟跑：检查所有持仓，触发止损/止盈"""
    today = datetime.now().strftime('%Y-%m-%d')
    now_hm = datetime.now().strftime('%H:%M')
    print(f"\n[{now_hm}] 🔍 盯盘扫描...")

    # 取今日决策
    dec = db.get_today_decision(today)
    if not dec:
        print("无今日决策记录"); return
    risk = json.loads(dec['risk_json']) if dec['risk_json'] else {}
    picks_map = {p['code']: p for p in (json.loads(dec['candidates_json']) if dec['candidates_json'] else [])}

    pos = trader.get_positions()
    active = [p for p in pos if p.get('count', 0) > 0 and p.get('availCount', 0) > 0]
    if not active:
        print("无可卖持仓"); return

    force_exit_time = risk.get('force_exit_time', '14:50')
    is_force_exit = now_hm >= force_exit_time

    for p in active:
        code = p['secCode']; name = p['secName']
        cost = p.get('_costPrice') or (p['costPrice'] / (10 ** p.get('costPriceDec', 2)))
        price = p.get('_price') or (p['price'] / (10 ** p.get('priceDec', 2)))
        if cost <= 0 or price <= 0: continue
        pnl_pct = (price - cost) / cost
        qty = p['availCount']

        pick = picks_map.get(code, {})
        sl = pick.get('stop_loss_pct', risk.get('max_drawdown_pct', -0.05))
        tp = pick.get('take_profit_pct', 0.06)

        reason = None
        if pnl_pct <= sl:
            reason = f'stop_loss ({pnl_pct*100:.2f}%)'
        elif pnl_pct >= tp:
            reason = f'take_profit ({pnl_pct*100:.2f}%)'
        elif is_force_exit:
            reason = f'time_exit ({now_hm})'

        print(f"  {code} {name} 成本{cost:.2f} 现价{price:.2f} 盈亏{pnl_pct*100:+.2f}% | sl={sl*100:.1f}% tp={tp*100:.1f}% | {reason or '持有'}")

        if reason:
            res = trader.sell(code, qty, price=None)  # 市价快速出
            order_id = res.get('data', {}).get('orderId') if isinstance(res.get('data'), dict) else ''
            status = 'submitted' if res.get('code') == '200' else 'failed'
            db.log_trade(today, code, name, 'SELL', price, qty, order_id, status, reason, dec['id'], res)
            db.log_signal(today, code, reason.split()[0], f"{name} {reason}")
            print(f"  ⛔ 卖出 → {res.get('code')} {res.get('message')}")
            time.sleep(1)

if __name__ == '__main__':
    check_and_exit()
