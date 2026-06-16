"""参数自动调优引擎 — 5道护栏 + 应用 + 回滚

护栏 1：TUNABLE 白名单（不在白名单的 key 拒绝）
护栏 2：BOUNDS 边界（超出 min/max 截断到边界并告警）
护栏 3：min_samples_to_tune 样本量门槛（< 阈值则跳过）
护栏 4：max_single_change_pct + max_3week_cumulative_pct 幅度限制
护栏 5：rollback 监控（由 rollback_watchdog 异步执行）

外部接口：
- propose_changes(metrics: dict) -> List[Patch]    # 由 reflect 调
- apply_patches(patches, source='reflector')       # 实际写入 + 落 params_history
- revert_last(scope=None) -> str                   # 一键回滚最近一次
- list_history(limit=20) -> list                   # 给微信/前端用
"""
import json
import importlib
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from pathlib import Path

import params as P
from db import get_conn

PARAMS_FILE = Path(__file__).parent / 'params.py'

# ---------- 工具：读写 params.py 中的 dict 字段 ----------

def _read_current(scope: str, key: str):
    """从 params 模块当前内存读值（运行时已加载）"""
    target = getattr(P, scope, None)
    if isinstance(target, dict):
        return target.get(key)
    return None


def _patch_params_file(scope: str, key: str, new_value) -> bool:
    """直接重写 params.py 中 SCOPE['key'] 的值（仅数字/布尔/字符串）

    使用文本替换 — 找 "    'key': <old>," 行替换。
    """
    src = PARAMS_FILE.read_text(encoding='utf-8')
    # 正则较脆，简单字符串扫描即可：从 "scope = {" 起到下一个 "}"
    import re
    pattern = re.compile(
        rf"({re.escape(scope)}\s*=\s*\{{[^}}]*?'{re.escape(key)}'\s*:\s*)([^,\n]+)(,)",
        re.DOTALL,
    )
    if not pattern.search(src):
        return False
    if isinstance(new_value, str):
        new_repr = f"'{new_value}'"
    else:
        new_repr = repr(new_value)
    new_src = pattern.sub(rf"\g<1>{new_repr}\g<3>", src, count=1)
    PARAMS_FILE.write_text(new_src, encoding='utf-8')
    # 抹除 .pyc 缓存，防止跨进程 import 读到旧字节码
    pyc = PARAMS_FILE.parent / '__pycache__' / f'{PARAMS_FILE.stem}.cpython-311.pyc'
    try:
        if pyc.exists():
            pyc.unlink()
    except Exception:
        pass
    return True


# ---------- 5 道护栏校验 ----------

class GuardError(Exception):
    pass


def _check_whitelist(scope: str, key: str):
    """护栏 1"""
    allowed = P.TUNABLE.get(scope, [])
    if key not in allowed:
        raise GuardError(f"参数 {scope}.{key} 不在 TUNABLE 白名单")


def _clip_bounds(key: str, value) -> Tuple[object, bool]:
    """护栏 2：返回 (clipped_value, was_clipped)"""
    if key not in P.BOUNDS:
        return value, False
    lo, hi = P.BOUNDS[key]
    clipped = max(lo, min(hi, value))
    return clipped, (clipped != value)


def _check_change_magnitude(scope: str, key: str, old, new) -> Tuple[bool, float, str]:
    """护栏 4：单次/累计幅度

    返回 (允许, change_pct, 原因)
    """
    max_single = P.REFLECTOR['max_single_change_pct']
    max_3week = P.REFLECTOR['max_3week_cumulative_pct']

    if old in (0, None) or not isinstance(old, (int, float)):
        return True, 0.0, ''

    change_pct = (new - old) / abs(old) if old else 0.0

    if abs(change_pct) > max_single:
        return False, change_pct, f"单次幅度 {change_pct*100:+.1f}% 超出 ±{max_single*100:.0f}%"

    # 累计 3 周
    c = get_conn()
    three_weeks_ago = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d %H:%M:%S')
    rows = c.execute(
        "SELECT change_pct FROM params_history "
        "WHERE scope=? AND param_key=? AND applied_at>=? AND rolled_back=0",
        (scope, key, three_weeks_ago),
    ).fetchall()
    c.close()

    cum = sum((r['change_pct'] or 0) for r in rows) + change_pct
    if abs(cum) > max_3week:
        return False, change_pct, f"3周累计 {cum*100:+.1f}% 超出 ±{max_3week*100:.0f}%"

    return True, change_pct, ''


# ---------- 主入口：apply_patches ----------

def apply_patches(
    patches: List[Dict],
    source: str = 'reflector',
    samples: int = 0,
    dry_run: bool = False,
) -> List[Dict]:
    """应用一批参数补丁

    patches: [{scope, key, new_value, reason}]
    返回 [{...patch, status: 'applied'|'rejected'|'clipped', detail}]

    护栏 3：samples < min_samples_to_tune → 全部 reject
    """
    results = []

    if samples < P.REFLECTOR['min_samples_to_tune']:
        for p in patches:
            results.append({**p, 'status': 'rejected',
                            'detail': f"样本不足 {samples}<{P.REFLECTOR['min_samples_to_tune']}"})
        return results

    for p in patches:
        scope = p['scope']
        key = p['key']
        new_value = p['new_value']
        reason = p.get('reason', '')

        try:
            _check_whitelist(scope, key)
        except GuardError as e:
            results.append({**p, 'status': 'rejected', 'detail': str(e)})
            continue

        old_value = _read_current(scope, key)
        if old_value is None:
            results.append({**p, 'status': 'rejected', 'detail': '原参数不存在'})
            continue

        # 护栏 2：边界裁剪
        clipped, was_clipped = _clip_bounds(key, new_value)

        # 护栏 4：幅度
        ok, change_pct, why = _check_change_magnitude(scope, key, old_value, clipped)
        if not ok:
            results.append({**p, 'status': 'rejected', 'detail': why})
            continue

        if dry_run:
            results.append({**p, 'old_value': old_value, 'new_value': clipped,
                            'status': 'dry_run', 'change_pct': change_pct,
                            'clipped': was_clipped})
            continue

        # 真正写入 params.py
        ok2 = _patch_params_file(scope, key, clipped)
        if not ok2:
            results.append({**p, 'status': 'rejected', 'detail': '写入 params.py 失败（未匹配到行）'})
            continue

        # 落库
        c = get_conn()
        c.execute(
            "INSERT INTO params_history(applied_at,source,scope,param_key,old_value,new_value,change_pct,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             source, scope, key, str(old_value), str(clipped),
             change_pct, reason),
        )
        c.commit(); c.close()

        # 热重载
        importlib.reload(P)

        status = 'clipped' if was_clipped else 'applied'
        results.append({**p, 'old_value': old_value, 'new_value': clipped,
                        'status': status, 'change_pct': change_pct})

    return results


def revert_last(scope: Optional[str] = None) -> str:
    """回滚最近一次（可指定 scope）"""
    c = get_conn()
    if scope:
        row = c.execute(
            "SELECT * FROM params_history WHERE rolled_back=0 AND scope=? "
            "ORDER BY id DESC LIMIT 1", (scope,)
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM params_history WHERE rolled_back=0 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not row:
        c.close()
        return "无可回滚的记录"

    # 把 new_value 改回 old_value
    old_str = row['old_value']
    # 类型还原
    try:
        old = json.loads(old_str)
    except Exception:
        try:
            old = float(old_str) if '.' in old_str else int(old_str)
        except Exception:
            old = old_str

    ok = _patch_params_file(row['scope'], row['param_key'], old)
    if not ok:
        c.close()
        return f"❌ 回滚失败：写入 params.py 未匹配 {row['scope']}.{row['param_key']}"

    c.execute("UPDATE params_history SET rolled_back=1, rolled_back_at=? WHERE id=?",
              (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
    c.commit(); c.close()
    importlib.reload(P)
    return f"✅ 已回滚 #{row['id']} {row['scope']}.{row['param_key']}: {row['new_value']} → {old}"


def list_history(limit: int = 20) -> List[Dict]:
    c = get_conn()
    rows = c.execute(
        "SELECT id,applied_at,source,scope,param_key,old_value,new_value,"
        "change_pct,reason,rolled_back FROM params_history "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'revert':
        print(revert_last(sys.argv[2] if len(sys.argv) > 2 else None))
    elif len(sys.argv) > 1 and sys.argv[1] == 'list':
        for r in list_history(20):
            mark = '🔄' if r['rolled_back'] else '✅'
            print(f"{mark} #{r['id']} {r['applied_at']} {r['scope']}.{r['param_key']}: "
                  f"{r['old_value']}→{r['new_value']} ({r['change_pct']*100:+.1f}%) {r['reason'][:40]}")
    else:
        print("usage: python param_engine.py [revert [scope]|list]")
