"""15:30 收盘复盘：拉成交 → 写日报 → 推微信"""
import os, json
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

import trader, db

def run_recap():
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"\n📊 [{today}] 收盘复盘\n{'='*60}")

    bal = trader.get_balance()
    bd = bal.get('data', {})
    total = bd.get('totalAssets', 0)
    pos_value = bd.get('totalPosValue', 0)
    avail = bd.get('availBalance', 0)

    pos = [p for p in trader.get_positions() if p.get('count',0) > 0]
    # 真相源：今日已成交订单（status=4 + 时间戳=今日）
    from datetime import time as dtime
    today_start = int(datetime.combine(datetime.now().date(), dtime(0,0)).timestamp())
    today_end = today_start + 86400
    real_orders = [o for o in trader.get_orders(0)
                   if o.get('status') == 4
                   and today_start <= (o.get('time') or 0) < today_end
                   and (o.get('tradeCount') or 0) > 0]

    # 今日交易记录（仅作对账参考）
    conn = db.get_conn()
    db_trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE date=? ORDER BY id", (today,)).fetchall()]
    conn.close()

    # ⚠️ 字段：drt=1买/2卖，tradePrice 需 ÷ 10**priceDec
    def avg_price(o):
        return (o.get('tradePrice',0) or 0) / (10 ** (o.get('priceDec',2) or 2))
    real_buys = [o for o in real_orders if o.get('drt') == 1]
    real_sells = [o for o in real_orders if o.get('drt') == 2]
    win = sum(1 for o in real_sells if (o.get('profit',0) or 0) > 0)
    loss = sum(1 for o in real_sells if (o.get('profit',0) or 0) < 0)

    # 当日盈亏 = 持仓 dayProfit 累加 + 已平仓 profit 累加
    day_profit = sum(p.get('dayProfit', 0) or 0 for p in pos) + sum(o.get('profit',0) or 0 for o in real_sells)
    day_pct = (day_profit / total * 100) if total else 0
    # 对账警告
    db_codes = {t['sec_code'] for t in db_trades if t['action']=='BUY'}
    real_codes = {p['secCode'] for p in pos} | {o['secCode'] for o in real_buys}
    ghost = db_codes - real_codes
    ghost_warn = f"\n⚠️ DB 有但实际未持仓（疑似下单被拒）: {','.join(ghost)}" if ghost else ""

    notes = f"实际建仓:{len(real_buys)} | 平仓:{len(real_sells)}（盈{win}/亏{loss}）| 持仓数:{len(pos)}"
    db.save_recap(today, total, day_profit, day_pct, win, loss, notes + ghost_warn)

    # 生成日报（用真实订单价/量）
    report = f"""<h3>📊 {today} 复盘</h3>
<p>💰 总资产 {total:,.0f}｜持仓 {pos_value:,.0f}｜可用 {avail:,.0f}｜当日盈亏 <strong>{day_profit:+,.0f}（{day_pct:+.2f}%）</strong></p>
<p>📦 {notes}</p>"""
    if ghost: report += f"<p>⚠️ <strong>下单被拒</strong>: {','.join(ghost)}（DB 有记录但实际无持仓）</p>"
    report += "<ul>"
    for o in real_orders:
        emoji = '🟢' if o.get('drt')==1 else '🔴'
        ap = avg_price(o)
        report += f"<li>{emoji} {o.get('secCode','')} {o.get('secName','')} 均价{ap:.2f}×{o.get('tradeCount',0)}</li>"
    report += "</ul>"

    # 持仓快照
    if pos:
        report += "<p><strong>当前持仓：</strong></p><ul>"
        for p in pos:
            if p.get('count',0)<=0: continue
            cost = p.get('_costPrice') or (p['costPrice'] / (10 ** p.get('costPriceDec', 2)))
            price = p.get('_price') or (p['price'] / (10 ** p.get('priceDec', 2)))
            pct = ((price-cost)/cost*100) if cost else 0
            report += f"<li>{p['secCode']} {p['secName']} 成本{cost:.2f} 现价{price:.2f} <strong>{pct:+.2f}%</strong></li>"
        report += "</ul>"

    print(report)
    
    # 发到妙想交流区
    try:
        res = trader.post_diary(report)
        print(f"📮 妙想发帖：{res.get('code')} {res.get('message')}")
    except Exception as e:
        print(f"⚠️  发帖失败：{e}")
    
    # 微信推送
    webhook = os.getenv('WECHAT_WEBHOOK', '')
    if webhook:
        import requests
        try:
            requests.post(webhook, json={'msgtype':'markdown','markdown':{'content':
                f"# 📊 {today} 复盘\n- 总资产 {total:,.0f}\n- 当日盈亏 **{day_profit:+,.0f}（{day_pct:+.2f}%）**\n- {notes}"}}, timeout=10)
            print("📱 微信推送成功")
        except Exception as e:
            print(f"微信推送失败：{e}")

    return {'total': total, 'profit': day_profit, 'pct': day_pct, 'notes': notes}

if __name__ == '__main__':
    run_recap()
