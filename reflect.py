"""周日 AI 反思 — 复盘本周战绩 + 优化下周策略 DSL

依赖：
- decision.chat()  → 复用 BYOK LLM，不引入 openai
- db.daily_recap / db.trades  → 历史数据源
- 自动建 reflections 表
"""
import os, json
from datetime import datetime, timedelta
from db import get_conn
from decision import chat
import notifier


def _ensure_reflections_table():
    c = get_conn()
    c.execute("""
    CREATE TABLE IF NOT EXISTS reflections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        content TEXT,
        win_rate REAL,
        trade_count INTEGER,
        net_pnl REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    c.commit(); c.close()


def _collect_week_data():
    """聚合最近 7 天 trades + recaps"""
    c = get_conn()
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    trades = c.execute(
        "SELECT date, sec_code, sec_name, action, price, quantity, status, reason "
        "FROM trades WHERE date>=? ORDER BY date", (week_ago,)
    ).fetchall()
    recaps = c.execute(
        "SELECT date, total_assets, day_profit, day_profit_pct, win_count, loss_count "
        "FROM daily_recap WHERE date>=? ORDER BY date", (week_ago,)
    ).fetchall()
    c.close()
    return [dict(t) for t in trades], [dict(r) for r in recaps]


def _compute_metrics(trades, recaps):
    """简单胜率/盈亏统计 — 配对 BUY/SELL 算单笔盈亏"""
    pairs = {}  # code -> last buy
    closed = []
    for t in trades:
        if t['status'] not in ('filled', 'submit_only'):
            continue
        if t['action'] == 'BUY':
            pairs[t['sec_code']] = t
        elif t['action'] == 'SELL' and t['sec_code'] in pairs:
            buy = pairs.pop(t['sec_code'])
            pnl_pct = (t['price'] - buy['price']) / buy['price'] if buy['price'] else 0
            closed.append({
                'code': t['sec_code'], 'name': t['sec_name'],
                'buy_price': buy['price'], 'sell_price': t['price'],
                'pnl_pct': round(pnl_pct, 4),
                'reason': t['reason']
            })
    win = sum(1 for c in closed if c['pnl_pct'] > 0)
    win_rate = win / len(closed) if closed else 0.0
    net = sum(r.get('day_profit', 0) or 0 for r in recaps)
    return {
        'closed_trades': closed,
        'open_positions': list(pairs.keys()),
        'win_rate': round(win_rate, 3),
        'trade_count': len(closed),
        'net_pnl': round(net, 2),
        'recaps': recaps,
    }


def weekly_reflect(silent: bool = False) -> str:
    _ensure_reflections_table()
    trades, recaps = _collect_week_data()

    if not trades and not recaps:
        msg = "本周无交易数据，跳过反思"
        if not silent:
            notifier.notify(msg, level='info', title='周复盘')
        return msg

    metrics = _compute_metrics(trades, recaps)

    prompt = f"""你是A股量化策略迭代专家。请基于本周战绩输出 JSON（仅 JSON，不要 markdown）：
{{
  "summary": "本周总结(<=80字)",
  "weak_points": ["弱点1", "弱点2"],
  "strong_points": ["亮点1"],
  "next_dsl_adjust": {{"keyword": "...", "reason": "..."}},
  "risk_adjust": {{"stop_loss_pct": -0.04, "take_profit_pct": 0.06, "reason": "..."}},
  "watch_list": ["可关注代码"]
}}

战绩数据：
- 交易笔数: {metrics['trade_count']} | 胜率: {metrics['win_rate']*100:.1f}% | 净盈亏: ¥{metrics['net_pnl']}
- 已平仓明细: {json.dumps(metrics['closed_trades'], ensure_ascii=False)[:1500]}
- 日复盘: {json.dumps(metrics['recaps'], ensure_ascii=False, default=str)[:800]}
- 持仓中: {metrics['open_positions']}
"""

    try:
        reflection = chat([{'role': 'user', 'content': prompt}], temperature=0.3)
    except Exception as e:
        err = f"❌ LLM 调用失败: {e}"
        if not silent:
            notifier.alert(err, level='warn', title='反思失败')
        return err

    # 落库
    c = get_conn()
    c.execute(
        "INSERT INTO reflections(date,content,win_rate,trade_count,net_pnl) VALUES(?,?,?,?,?)",
        (datetime.now().strftime('%Y-%m-%d'), reflection,
         metrics['win_rate'], metrics['trade_count'], metrics['net_pnl'])
    )
    c.commit(); c.close()

    if not silent:
        title = f"周复盘 胜率{metrics['win_rate']*100:.0f}% 净盈亏¥{metrics['net_pnl']:.0f}"
        # 截断保护，飞书/微信都怕长
        notifier.notify(f"📝 {title}\n{reflection[:600]}", level='info', title=title)

    return reflection


if __name__ == "__main__":
    import sys
    silent = '--silent' in sys.argv
    print(weekly_reflect(silent=silent))
