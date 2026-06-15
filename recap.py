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

    pos = trader.get_positions()
    today_orders = [o for o in trader.get_orders(0) if o.get('status') == 4]  # 已成

    # 今日交易记录
    conn = db.get_conn()
    today_trades = conn.execute(
        "SELECT * FROM trades WHERE date=? ORDER BY id", (today,)).fetchall()
    today_trades = [dict(r) for r in today_trades]
    conn.close()

    win = sum(1 for t in today_trades if t['action']=='SELL' and (t.get('reason','').startswith('take_profit')))
    loss = sum(1 for t in today_trades if t['action']=='SELL' and (t.get('reason','').startswith('stop_loss')))
    
    # 今日盈亏（粗算：日间持仓盈亏总和）
    day_profit = sum(p.get('dayProfit', 0) for p in pos)
    day_pct = (day_profit / total * 100) if total else 0

    notes = f"建仓:{sum(1 for t in today_trades if t['action']=='BUY')} | 止盈:{win} | 止损:{loss} | 持仓数:{len([p for p in pos if p.get('count',0)>0])}"
    db.save_recap(today, total, day_profit, day_pct, win, loss, notes)

    # 生成日报
    report = f"""<h3>📊 {today} 复盘</h3>
<p>💰 总资产 {total:,.0f}｜持仓 {pos_value:,.0f}｜当日盈亏 <strong>{day_profit:+,.0f}（{day_pct:+.2f}%）</strong></p>
<p>📦 {notes}</p>
<ul>"""
    for t in today_trades:
        emoji = '🟢' if t['action']=='BUY' else '🔴'
        report += f"<li>{emoji} {t['action']} {t['sec_code']} {t.get('sec_name','')} 价{t.get('price',0):.2f}×{t['quantity']} | {t.get('reason','')}</li>"
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
