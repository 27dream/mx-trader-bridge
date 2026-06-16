"""主流程：AI决策 → 妙想下单（含白名单 + 风控预检 + 成交校验 + 多通道告警）"""
import os, sys, json, requests, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

import trader, db, decision as dm, risk_guard, notifier

POSITION_COUNT = int(os.getenv('POSITION_COUNT', 2))
POSITION_PCT = float(os.getenv('POSITION_PCT', 0.5))
FILL_POLL_TIMES = int(os.getenv('FILL_POLL_TIMES', 5))   # 成交轮询次数
FILL_POLL_INTERVAL = float(os.getenv('FILL_POLL_INTERVAL', 2.0))


def get_realtime_price(stock_code: str) -> float:
    """从腾讯接口获取实时价"""
    prefix = 'sh' if stock_code.startswith(('60', '68', '90')) else 'sz'
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={prefix}{stock_code}", timeout=5)
        text = r.text.encode('latin-1').decode('gbk')
        parts = text.split('~')
        if len(parts) > 4:
            return float(parts[3])
    except: pass
    return 0.0


def calc_quantity(available_cash: float, price: float, pct: float = POSITION_PCT) -> int:
    """按仓位计算可买股数（100整数倍）"""
    budget = available_cash * pct
    raw = int(budget / price)
    return (raw // 100) * 100


def build_whitelist() -> set:
    """构建当日合法股票池（防 LLM 幻觉）

    来源：昨日涨停池 + 今日涨幅榜 + 强势股回调候选
    任何不在白名单的 code 一律拒绝下单
    """
    pool = set()
    try:
        for s in dm.get_zt_pool() or []:
            if s.get('code'): pool.add(s['code'])
        for s in dm.get_hot_stocks(80) or []:
            if s.get('code'): pool.add(s['code'])
    except Exception as e:
        print(f"⚠️  白名单构建异常: {e}")
    return pool


def verify_filled(stock_code: str, expected_qty: int, max_wait: float = None) -> dict:
    """轮询 orders 接口确认订单 status=4（已成）

    Returns: {'filled': bool, 'order_id': str, 'fill_price': float, 'fill_qty': int}
    """
    if max_wait is None:
        max_wait = FILL_POLL_TIMES * FILL_POLL_INTERVAL
    deadline = time.time() + max_wait
    last_order = None
    while time.time() < deadline:
        try:
            orders = trader.get_orders(0)  # 全部状态
            today_start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
            for o in orders:
                if o.get('secCode') != stock_code: continue
                if (o.get('time') or 0) < today_start: continue
                if o.get('drt') != 1: continue   # 1=买
                last_order = o
                if o.get('status') == 4 and (o.get('tradeCount') or 0) > 0:
                    pdec = o.get('priceDec', 0)
                    fp = (o.get('tradePrice') or 0) / (10 ** pdec) if pdec else 0
                    return {
                        'filled': True,
                        'order_id': o.get('id') or o.get('orderId', ''),
                        'fill_price': fp,
                        'fill_qty': o.get('tradeCount', 0),
                    }
        except Exception as e:
            print(f"   ⚠️  查 orders 异常: {e}")
        time.sleep(FILL_POLL_INTERVAL)
    # 超时未成交
    return {
        'filled': False,
        'order_id': (last_order or {}).get('id') or (last_order or {}).get('orderId', ''),
        'fill_price': 0,
        'fill_qty': 0,
        'last_status': (last_order or {}).get('status'),
    }


def run_morning_trade():
    """09:30 开盘前/后执行：生成决策 → 白名单过滤 → 下单 → 成交校验"""
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

    # 3. 构建股票白名单（防 LLM 幻觉）
    whitelist = build_whitelist()
    print(f"✅ 合法股票池：{len(whitelist)} 只（来自妙想真实选股）")
    if len(whitelist) < 5:
        print("❌ 白名单太小，可能 mx_screen 失败，停止下单"); return

    # 4. AI 生成决策
    print("🧠 调用 AI 生成今日策略...")
    decision = dm.generate_decision()
    if not decision or not decision.get('picks'):
        notifier.alert("❌ 决策生成失败，建仓流程终止", level='warn', title='决策失败')
        return
    print(f"📋 策略：{decision.get('strategy_name')} | 大盘：{decision.get('market_view', '')}")
    notifier.notify_decision(
        decision.get('strategy_name', ''),
        decision.get('picks', []),
        decision.get('market_view', '')
    )

    # 5. 落库
    decision_id = db.log_decision(today, decision.get('strategy_name', ''),
        decision, decision.get('global_risk', {}),
        decision.get('picks', []), decision.get('ai_reasoning', ''))

    # 6. 白名单过滤 picks
    raw_picks = decision['picks']
    valid_picks, rejected = [], []
    for p in raw_picks:
        code = p.get('code', '')
        if code in whitelist:
            valid_picks.append(p)
        else:
            rejected.append(code)
    if rejected:
        print(f"🚫 LLM 幻觉拦截: {rejected}（不在合法池中，已拒绝）")
        for code in rejected:
            db.log_signal(today, code, 'HALLUCINATION_BLOCKED',
                          f'LLM 输出股票 {code} 不在合法池中，拒绝下单')
    if not valid_picks:
        print("❌ 经白名单过滤后无可下单标的"); return

    # 7. 逐只下单 + 成交校验
    picks = valid_picks[:free_slots]
    cash_per_pos = avail / free_slots
    for pick in picks:
        code = pick['code']
        name = pick.get('name', code)
        if any(p['secCode'] == code for p in active_pos):
            print(f"⏭️  {code} {name} 已持仓，跳过"); continue

        rt_price = get_realtime_price(code)
        if rt_price <= 0:
            print(f"⚠️  {code} 取价失败，跳过（避免市价单不可控）")
            db.log_signal(today, code, 'PRICE_FETCH_FAILED', '实时价获取失败，跳过下单')
            continue

        # 限价：当前价 +0.5% 提高成交概率
        limit_price = round(rt_price * 1.005, 2)
        qty = calc_quantity(cash_per_pos, limit_price, 1.0)
        if qty < 100:
            print(f"⚠️  {code} 仓位不足以买100股 (price={limit_price})，跳过"); continue
        print(f"🛒 买入 {code} {name} | 限价 {limit_price} | 数量 {qty} | 占用 ¥{limit_price*qty:,.0f}")

        # ✅ 风控预检（mx-risk-guard：黑名单/熔断/资金/集中度）
        rg = risk_guard.pre_check_buy(code, qty, limit_price, balance=bal, positions=pos)
        if not rg['ok']:
            print(f"   🛡️ 风控拒单: {rg['reason']} | {rg['detail']}")
            notifier.notify_reject(code, name, f"{rg['reason']}: {rg['detail']}")
            db.log_signal(today, code, f"RISK_REJECT_{rg['reason'].upper()}",
                          json.dumps(rg['detail'], ensure_ascii=False))
            db.log_trade(today, code, name, 'BUY', limit_price, qty, '',
                         'risk_rejected', f"risk_guard:{rg['reason']}",
                         decision_id, rg)
            continue
        print(f"   ✅ 风控通过 | 预测仓位 {rg['detail'].get('predicted_pct',0)*100:.1f}% | 余额 ¥{rg['detail'].get('avail_after',0):,.0f}")

        # 提交订单（trader._trade 内部已 rc=0 校验，rc!=0 抛 TradeError）
        try:
            res = trader.buy(code, qty, price=limit_price)
            order_id = trader._extract_order_id(res)
            print(f"   ✅ 已提交 orderId={order_id}")
        except trader.TradeError as e:
            print(f"   ❌ 下单被拒: {e}")
            notifier.notify_reject(code, name, str(e)[:200])
            db.log_trade(today, code, name, 'BUY', limit_price, qty, '',
                         'rejected', f"strategy:{decision.get('strategy_name')}/rejected",
                         decision_id, {'error': str(e)})
            continue

        # 轮询确认成交
        print(f"   ⏳ 等待成交...")
        result = verify_filled(code, qty)
        if result['filled']:
            db.log_trade(today, code, name, 'BUY',
                         result['fill_price'], result['fill_qty'],
                         result['order_id'], 'filled',
                         f"strategy:{decision.get('strategy_name')}",
                         decision_id, res)
            print(f"   🎯 已成交 ¥{result['fill_price']:.2f} × {result['fill_qty']}")
            notifier.notify_fill(code, name, 'BUY',
                                 result['fill_price'], result['fill_qty'],
                                 decision.get('strategy_name', ''))
        else:
            db.log_trade(today, code, name, 'BUY', limit_price, qty,
                         result['order_id'], 'pending',
                         f"strategy:{decision.get('strategy_name')}/timeout",
                         decision_id, res)
            print(f"   ⏰ {FILL_POLL_TIMES*FILL_POLL_INTERVAL}s 内未成交（last_status={result.get('last_status')}），后续 monitor 继续跟踪")
            notifier.alert(f"⏰ 买单未成交 {code} {name} ¥{limit_price} × {qty}（status={result.get('last_status')}）",
                           level='warn', title='买单超时')
        time.sleep(1)

    print(f"\n✅ 建仓完成。决策ID={decision_id}")
    notifier.notify(f"✅ 建仓流程结束｜决策ID={decision_id}", level='success', title='建仓完成')

if __name__ == '__main__':
    db.init_db()
    run_morning_trade()
