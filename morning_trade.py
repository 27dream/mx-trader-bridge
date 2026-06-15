"""主流程：AI决策 → 妙想下单"""
import os, sys, json, requests, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

import trader, db, decision as dm

POSITION_COUNT = int(os.getenv('POSITION_COUNT', 2))
POSITION_PCT = float(os.getenv('POSITION_PCT', 0.5))

def get_realtime_price(stock_code: str) -> float:
    """从腾讯接口获取实时价"""
    prefix = 'sh' if stock_code.startswith(('60', '68', '90')) else 'sz'
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={prefix}{stock_code}", timeout=5)
        text = r.text.encode('latin-1').decode('gbk')
        parts = text.split('~')
        if len(parts) > 4:
            return float(parts[3])  # 当前价
    except: pass
    return 0.0

def calc_quantity(available_cash: float, price: float, pct: float = POSITION_PCT) -> int:
    """按仓位计算可买股数（100整数倍）"""
    budget = available_cash * pct
    raw = int(budget / price)
    return (raw // 100) * 100

def run_morning_trade():
    """09:30 开盘前/后执行：生成决策 → 下单"""
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"\n{'='*60}\n🌅 [{today}] 开盘建仓流程启动\n{'='*60}")

    # 1. 检查账户
    bal = trader.get_balance()
    if bal.get('code') != '200':
        print(f"❌ 余额查询失败: {bal}"); return
    avail = bal['data']['availBalance']
    print(f"💰 可用余额: {avail:,.2f} 元")

    # 2. 检查现有持仓数
    pos = trader.get_positions()
    active_pos = [p for p in pos if p.get('count', 0) > 0]
    free_slots = POSITION_COUNT - len(active_pos)
    print(f"📦 当前持仓 {len(active_pos)}/{POSITION_COUNT}，剩余仓位 {free_slots}")
    if free_slots <= 0:
        print("⚠️  仓位已满，跳过建仓"); return

    # 3. AI 生成决策
    print("🧠 调用 AI 生成今日策略...")
    decision = dm.generate_decision()
    if not decision or not decision.get('picks'):
        print("❌ 决策生成失败"); return
    print(f"📋 策略：{decision.get('strategy_name')} | 大盘：{decision.get('market_view', '')}")

    # 4. 落库
    decision_id = db.log_decision(today, decision.get('strategy_name', ''),
        decision, decision.get('global_risk', {}),
        decision.get('picks', []), decision.get('ai_reasoning', ''))

    # 5. 逐只下单
    picks = decision['picks'][:free_slots]
    cash_per_pos = avail / free_slots
    for pick in picks:
        code = pick['code']
        name = pick.get('name', code)
        # 跳过已持仓
        if any(p['secCode'] == code for p in active_pos):
            print(f"⏭️  {code} {name} 已持仓，跳过"); continue

        rt_price = get_realtime_price(code)
        if rt_price <= 0:
            print(f"⚠️  {code} 取价失败，使用市价单")
            qty = calc_quantity(cash_per_pos, 50, 1.0)  # 估算
            res = trader.buy(code, qty, price=None)
        else:
            # 限价：当前价 +0.5% 提高成交概率
            limit_price = round(rt_price * 1.005, 2)
            qty = calc_quantity(cash_per_pos, limit_price, 1.0)
            if qty < 100:
                print(f"⚠️  {code} 仓位不足以买100股 (price={limit_price})，跳过"); continue
            print(f"🛒 买入 {code} {name} | 限价 {limit_price} | 数量 {qty} | 占用 ¥{limit_price*qty:,.0f}")
            res = trader.buy(code, qty, price=limit_price)

        order_id = res.get('data', {}).get('orderId') if isinstance(res.get('data'), dict) else ''
        status = 'submitted' if res.get('code') == '200' else 'failed'
        db.log_trade(today, code, name, 'BUY',
            limit_price if rt_price > 0 else 0, qty, order_id, status,
            f"strategy:{decision.get('strategy_name')}", decision_id, res)
        print(f"   → {res.get('code')} {res.get('message')}")
        time.sleep(1)

    print(f"\n✅ 建仓完成。决策ID={decision_id}")

if __name__ == '__main__':
    db.init_db()
    run_morning_trade()
