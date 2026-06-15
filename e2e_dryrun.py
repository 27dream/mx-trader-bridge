"""端到端 dry-run：在不下真单的前提下验证全链路通畅

校验链：
  1. 配置加载 (config_store)
  2. LLM 连通 (decision.chat)
  3. 妙想行情 + 账户查询 (trader.get_balance / get_positions)
  4. 选股 → 决策 (decision.generate_decision)
  5. 风控预检 (risk_guard.pre_check_buy)  ← 不下真单
  6. 告警通道 (notifier.notify)
  7. 数据落库读取 (db.log_decision / get_today_decision)
  8. 复盘聚合 (recap)
  9. 反思 (reflect — silent 模式)

任何一步失败都会即时报告，不会污染真实交易。
"""
import os, sys, json, traceback
from datetime import datetime
os.environ['DRY_RUN'] = '1'  # 给依赖模块一个识别信号

OK = '✅'; FAIL = '❌'; WARN = '⚠️'

results = []
def step(name, fn):
    print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
    try:
        fn()
        results.append((name, 'PASS', ''))
        print(f"{OK} {name} 通过")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        results.append((name, 'FAIL', msg))
        print(f"{FAIL} {name} 失败 → {msg}")
        traceback.print_exc()


def s_config():
    from config_store import load
    cfg = load()
    print({
        'llm_model': cfg.get('llm_model'),
        'llm_base_url': cfg.get('llm_base_url'),
        'mx_cookie': '***已配置' if (cfg.get('mx_cookie') or cfg.get('mx_cookies')) else None,
    })
    if not cfg.get('llm_model') or not cfg.get('llm_base_url'):
        raise RuntimeError("缺少 LLM 配置（llm_model / llm_base_url）")
    # mx_cookie 不强校验 — 实际能否调通由后续步骤 3 验证


def s_llm():
    from decision import chat
    out = chat([{'role': 'user', 'content': '回答 OK 一个字'}], temperature=0)
    print(f"  LLM 回复: {out[:80]}")
    if not out:
        raise RuntimeError("LLM 返回空")


def s_broker():
    import trader
    bal = trader.get_balance()
    pos = trader.get_positions()
    bd = bal.get('data') or {}
    print(f"  总资产 ¥{bd.get('totalAssets', 0):,.0f} | 可用 ¥{bd.get('availBalance', 0):,.0f} | 持仓 {len(pos)} 只")
    for p in pos[:3]:
        print(f"    · {p.get('secCode')} {p.get('secName')} 数量 {p.get('count')}")


def s_decision():
    from decision import generate_decision
    d = generate_decision()
    if not d:
        raise RuntimeError("decision.generate_decision 返回空")
    picks = d.get('picks', [])
    print(f"  策略: {d.get('strategy_name')} | 大盘: {d.get('market_view', '')[:40]} | 候选 {len(picks)} 只")
    for p in picks[:3]:
        print(f"    · {p.get('code')} {p.get('name')} sl={p.get('stop_loss_pct')} tp={p.get('take_profit_pct')}")
    # 暂存到全局供下一步用
    globals()['_decision_cache'] = d


def s_risk_check():
    import risk_guard, trader
    d = globals().get('_decision_cache') or {}
    picks = d.get('picks', [])
    if not picks:
        print(f"  {WARN} 无候选标的，跳过风控预检")
        return
    bal = trader.get_balance(); pos = trader.get_positions()
    for p in picks[:2]:
        # 假设以 10 元限价、1 手数量做预检（不真实下单）
        rg = risk_guard.pre_check_buy(p['code'], 100, 10.0, balance=bal, positions=pos)
        flag = OK if rg['ok'] else WARN
        print(f"  {flag} {p['code']} 风控: ok={rg['ok']} reason={rg['reason']} detail={rg['detail']}")


def s_notify():
    import notifier
    r = notifier.notify("e2e_dryrun 自检消息（请忽略）", level='info', title='dry-run')
    print(f"  通道结果: {r}")


def s_db():
    import db
    db.init_db()
    today = datetime.now().strftime('%Y-%m-%d')
    # 写一条 dry-run 决策记录，再读取
    did = db.log_decision(today + '_dryrun', 'DRYRUN', {}, {}, [], 'e2e_dryrun stub')
    rec = db.get_today_decision(today + '_dryrun')
    if not rec:
        raise RuntimeError("决策落库后查询不到")
    print(f"  decision_id={did} 已落库并读回 ✓")


def s_recap():
    try:
        import recap
        if hasattr(recap, 'daily_recap'):
            print("  recap.daily_recap 函数存在 ✓（dry-run 跳过执行）")
        else:
            print(f"  {WARN} recap 模块无 daily_recap 函数")
    except ImportError as e:
        print(f"  {WARN} recap 不可用: {e}")


def s_reflect():
    from reflect import weekly_reflect
    out = weekly_reflect(silent=True)
    print(f"  reflect 输出（前 200 字）: {out[:200]}")


def main():
    print(f"\n🧪 mx-trader-bridge e2e dry-run @ {datetime.now()}\n")
    step("1. 配置加载", s_config)
    step("2. LLM 连通", s_llm)
    step("3. 妙想行情 + 账户", s_broker)
    step("4. AI 决策生成", s_decision)
    step("5. 风控预检 (pre_check_buy)", s_risk_check)
    step("6. 告警通道", s_notify)
    step("7. 数据库读写", s_db)
    step("8. recap 模块", s_recap)
    step("9. reflect 周复盘", s_reflect)

    print("\n" + "=" * 60)
    print("📋 e2e_dryrun 汇总")
    print("=" * 60)
    pass_cnt = sum(1 for _, s, _ in results if s == 'PASS')
    fail_cnt = sum(1 for _, s, _ in results if s == 'FAIL')
    for name, status, err in results:
        icon = OK if status == 'PASS' else FAIL
        print(f"  {icon} {name}" + (f"  → {err}" if err else ''))
    print(f"\n通过 {pass_cnt}/{len(results)}  失败 {fail_cnt}")
    sys.exit(0 if fail_cnt == 0 else 1)


if __name__ == '__main__':
    main()
