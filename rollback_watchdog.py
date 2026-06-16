"""调参回滚守护 — 跟踪 reflector 应用的参数 N 天，劣化则自动 revert

触发条件（任一满足即回滚）：
1. 应用后样本期胜率 ≤ 应用前胜率 - degrade_threshold_pp（默认 10pp）
2. 应用后样本期日均盈亏 < 0 且 < 应用前的 min_pnl_ratio 倍（默认 0.5x）

只跟踪：
- source='reflector' 的记录
- rolled_back=0
- applied_at 在过去 watch_days 天内（默认 7）
- 至少有 min_samples_after 笔已平仓单（默认 5）

调度：每日 17:00 通过 cron 跑一次（独立于 weekly_reflect）
"""
from datetime import datetime, timedelta
from typing import Optional
from db import get_conn
import param_engine as pe
import params as P
import notifier


# 默认配置（可覆盖）
WATCH_DAYS = 7
DEGRADE_WIN_PP = 0.10            # 胜率下降 10pp 触发
MIN_SAMPLES_AFTER = 5            # 应用后至少 5 笔平仓
PNL_RATIO_THRESHOLD = 0.5        # 应用后日均盈亏 < 应用前 × 0.5 触发（同时盈亏需为负）


def _stats_in_range(start_ts: str, end_ts: Optional[str] = None):
    """统计区间内的胜率 + 日均盈亏"""
    c = get_conn()
    end_clause = "AND date<=?" if end_ts else ""
    args = [start_ts] + ([end_ts] if end_ts else [])

    trades = c.execute(
        f"SELECT date,sec_code,action,price,status FROM trades "
        f"WHERE date>=? {end_clause} ORDER BY date", args,
    ).fetchall()
    recaps = c.execute(
        f"SELECT date,day_profit FROM daily_recap "
        f"WHERE date>=? {end_clause}", args,
    ).fetchall()
    c.close()

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
            closed.append(pnl_pct)

    win = sum(1 for p in closed if p > 0)
    win_rate = win / len(closed) if closed else 0.0
    avg_pnl = sum((r['day_profit'] or 0) for r in recaps) / max(len(recaps), 1)
    return {
        'samples': len(closed),
        'win_rate': win_rate,
        'avg_daily_pnl': avg_pnl,
        'days': len(recaps),
    }


def _evaluate_patch(row: dict) -> Optional[str]:
    """评估单条 params_history 是否需要回滚

    返回回滚原因（字符串）或 None（保留）
    """
    applied_at = row['applied_at']
    # 应用前 N 天
    before_start = (datetime.strptime(applied_at, '%Y-%m-%d %H:%M:%S')
                    - timedelta(days=WATCH_DAYS)).strftime('%Y-%m-%d')
    before_end = applied_at[:10]
    before = _stats_in_range(before_start, before_end)

    # 应用后到现在
    after = _stats_in_range(applied_at[:10])

    if after['samples'] < MIN_SAMPLES_AFTER:
        return None  # 样本不足，继续观察

    reasons = []

    # 触发 1：胜率劣化
    if before['samples'] >= 3:
        win_drop = before['win_rate'] - after['win_rate']
        if win_drop >= DEGRADE_WIN_PP:
            reasons.append(
                f"胜率劣化 {before['win_rate']*100:.0f}%→{after['win_rate']*100:.0f}% "
                f"(-{win_drop*100:.0f}pp)"
            )

    # 触发 2：盈亏劣化
    if after['avg_daily_pnl'] < 0:
        if before['avg_daily_pnl'] > 0:
            reasons.append(
                f"由盈转亏: ¥{before['avg_daily_pnl']:.0f}→¥{after['avg_daily_pnl']:.0f}/日"
            )
        elif before['avg_daily_pnl'] < 0 and after['avg_daily_pnl'] < before['avg_daily_pnl'] * (1/PNL_RATIO_THRESHOLD):
            # 应用前-100, 应用后-300（更亏 3x）
            reasons.append(
                f"亏损放大: ¥{before['avg_daily_pnl']:.0f}→¥{after['avg_daily_pnl']:.0f}/日"
            )

    return " | ".join(reasons) if reasons else None


def watchdog_run(silent: bool = False) -> dict:
    """主入口 — 扫描所有未回滚的 reflector 记录，劣化则回滚"""
    c = get_conn()
    cutoff = (datetime.now() - timedelta(days=WATCH_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    rows = c.execute(
        "SELECT id,applied_at,scope,param_key,old_value,new_value,change_pct,reason "
        "FROM params_history "
        "WHERE source='reflector' AND rolled_back=0 AND applied_at>=? "
        "ORDER BY id DESC", (cutoff,)
    ).fetchall()
    c.close()

    if not rows:
        return {'checked': 0, 'reverted': 0, 'kept': 0}

    reverted = []
    kept = []
    for r in rows:
        rdict = dict(r)
        reason = _evaluate_patch(rdict)
        if reason:
            # 通过 param_engine 反向写入
            tag = f"#{rdict['id']} {rdict['scope']}.{rdict['param_key']}"
            try:
                # 直接复用 revert_last 不行（它取最近的），手工调用底层
                from param_engine import _patch_params_file
                # 还原类型
                old_str = rdict['old_value']
                try:
                    import json as _j
                    old_val = _j.loads(old_str)
                except Exception:
                    try:
                        old_val = float(old_str) if '.' in old_str else int(old_str)
                    except Exception:
                        old_val = old_str

                ok = _patch_params_file(rdict['scope'], rdict['param_key'], old_val)
                if ok:
                    c2 = get_conn()
                    c2.execute(
                        "UPDATE params_history SET rolled_back=1, rolled_back_at=? WHERE id=?",
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rdict['id']),
                    )
                    c2.commit(); c2.close()
                    import importlib
                    importlib.reload(P)
                    reverted.append({'tag': tag, 'reason': reason,
                                     'old': rdict['new_value'], 'new': str(old_val)})
                else:
                    kept.append({'tag': tag, 'reason': '回滚写入失败'})
            except Exception as e:
                kept.append({'tag': tag, 'reason': f'回滚异常: {e}'})
        else:
            kept.append({'tag': f"#{rdict['id']} {rdict['scope']}.{rdict['param_key']}",
                         'reason': '观察中'})

    # 推送
    if reverted and not silent:
        lines = [f"🔄 自动回滚 {len(reverted)} 项（劣化触发）"]
        for r in reverted:
            lines.append(f"  • {r['tag']}: {r['old']}→{r['new']}")
            lines.append(f"    原因: {r['reason']}")
        notifier.alert("\n".join(lines), level='warn', title='调参守护回滚')

    return {
        'checked': len(rows),
        'reverted': len(reverted),
        'kept': len(kept),
        'reverted_detail': reverted,
        'kept_detail': kept,
    }


if __name__ == '__main__':
    import sys, json as _j
    silent = '--silent' in sys.argv
    result = watchdog_run(silent=silent)
    print(_j.dumps(result, ensure_ascii=False, indent=2, default=str))
