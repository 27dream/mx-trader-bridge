"""周日 AI 反思 + 自动调参 v2

改造点（vs v1）：
- LLM 输出标准 patches[] (scope/key/new_value/reason)
- 自动通过 param_engine.apply_patches 跑 5 道护栏
- 推送含调参明细 + 命中/拒绝原因
- REFLECTOR.auto_apply_param_patch=False 时仅 dry_run

依赖：
- decision.chat → BYOK LLM
- db.daily_recap / db.trades → 历史数据源
- param_engine → 5护栏 + 落 params_history
- params.REFLECTOR → 自动调参开关
"""
import os, json, re
from datetime import datetime, timedelta
from db import get_conn
from decision import chat
import notifier
import params as P
import param_engine as pe


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
        patches_applied INTEGER DEFAULT 0,
        patches_rejected INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # 兼容旧表 — 缺列就补
    cols = {r['name'] for r in c.execute("PRAGMA table_info(reflections)").fetchall()}
    if 'patches_applied' not in cols:
        c.execute("ALTER TABLE reflections ADD COLUMN patches_applied INTEGER DEFAULT 0")
    if 'patches_rejected' not in cols:
        c.execute("ALTER TABLE reflections ADD COLUMN patches_rejected INTEGER DEFAULT 0")
    c.commit(); c.close()


def _collect_week_data():
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
    pairs = {}
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


def _build_tunable_doc():
    """生成给 LLM 看的可调参数白名单 + 当前值 + 边界"""
    lines = []
    for scope, keys in P.TUNABLE.items():
        scope_dict = getattr(P, scope, {})
        for k in keys:
            cur = scope_dict.get(k)
            bnd = P.BOUNDS.get(k)
            bnd_str = f"[{bnd[0]} ~ {bnd[1]}]" if bnd else "无界"
            lines.append(f"  - {scope}.{k} = {cur}  范围{bnd_str}")
    return "\n".join(lines)


def _parse_llm_json(text: str) -> dict:
    """LLM 返回有时带 ```json 包裹，剥掉"""
    if not text:
        return {}
    # 去 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 找第一个 { 到最后一个 }
    s = text.find('{')
    e = text.rfind('}')
    if s >= 0 and e > s:
        text = text[s:e+1]
    try:
        return json.loads(text)
    except Exception:
        return {}


def weekly_reflect(silent: bool = False, force_dry_run: bool = False) -> str:
    _ensure_reflections_table()
    trades, recaps = _collect_week_data()

    if not trades and not recaps:
        msg = "本周无交易数据，跳过反思"
        if not silent:
            notifier.notify(msg, level='info', title='周复盘')
        return msg

    metrics = _compute_metrics(trades, recaps)
    tunable_doc = _build_tunable_doc()

    prompt = f"""你是A股量化策略迭代专家。基于本周战绩，输出严格 JSON（仅 JSON，无 markdown）：

{{
  "summary": "本周总结(<=80字)",
  "weak_points": ["弱点1"],
  "strong_points": ["亮点1"],
  "patches": [
    {{"scope": "STRATEGY_A", "key": "sl_pct", "new_value": -0.025, "reason": "本周A策略胜率低,放宽止损"}},
    {{"scope": "STRATEGY_B", "key": "max_picks", "new_value": 2, "reason": "..."}}
  ],
  "watch_list": ["可关注代码"]
}}

⚠️ 硬约束（违反则补丁会被拒）：
1. patches 中的 scope.key 必须出自下方白名单
2. new_value 必须在标注的范围内
3. 单次调整幅度 ≤ ±15%（相对当前值）
4. patches 可以为空数组，表示本周维持现状最优

可调参数白名单（当前值 + 范围）：
{tunable_doc}

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

    parsed = _parse_llm_json(reflection)
    patches = parsed.get('patches', []) or []

    # 自动应用（或 dry_run）
    auto_apply = P.REFLECTOR.get('auto_apply_param_patch', False) and not force_dry_run
    apply_results = []
    if patches:
        apply_results = pe.apply_patches(
            patches,
            source='reflector',
            samples=metrics['trade_count'],
            dry_run=not auto_apply,
        )

    applied = sum(1 for r in apply_results if r['status'] in ('applied', 'clipped'))
    rejected = sum(1 for r in apply_results if r['status'] == 'rejected')

    # 落库
    c = get_conn()
    c.execute(
        "INSERT INTO reflections(date,content,win_rate,trade_count,net_pnl,"
        "patches_applied,patches_rejected) VALUES(?,?,?,?,?,?,?)",
        (datetime.now().strftime('%Y-%m-%d'), reflection,
         metrics['win_rate'], metrics['trade_count'], metrics['net_pnl'],
         applied, rejected)
    )
    c.commit(); c.close()

    # 组装推送
    summary = parsed.get('summary', reflection[:80])
    lines = [f"📝 {summary}",
             f"📊 笔数{metrics['trade_count']} 胜率{metrics['win_rate']*100:.0f}% 盈亏¥{metrics['net_pnl']:.0f}"]
    if apply_results:
        mode = "已应用" if auto_apply else "DRY-RUN"
        lines.append(f"\n🔧 调参 [{mode}] {applied}通过 / {rejected}拒绝")
        for r in apply_results[:8]:
            icon = {'applied':'✅','clipped':'⚠️','dry_run':'🟦','rejected':'❌'}.get(r['status'],'·')
            tag = f"{r['scope']}.{r['key']}"
            if r['status'] in ('applied','clipped','dry_run'):
                lines.append(f"  {icon} {tag}: {r.get('old_value')} → {r.get('new_value')}")
            else:
                lines.append(f"  {icon} {tag}: {r.get('detail','')[:50]}")
    else:
        lines.append("\n🔧 本周无参数调整建议")

    if not silent:
        notifier.notify("\n".join(lines), level='info', title=f"周复盘+自动调参")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    silent = '--silent' in sys.argv
    dry = '--dry-run' in sys.argv
    print(weekly_reflect(silent=silent, force_dry_run=dry))
