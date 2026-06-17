"""选股模块 — A+B+C 三策略候选生成

A. 强势股回调：近20日涨幅>15% + 5日均线上 + 缩量回调到MA10
B. 首板跟进：昨日首板 + 流通市值30-150亿 + 非ST
C. 主力净流入：当日主力净流入>5000万 + 量比>2

每天09:00跑一次，输出 candidates.json
"""
import os, json, time, sqlite3
from datetime import datetime, timedelta
from typing import List, Dict
import requests
from config_store import load as _load_cfg

_cfg = _load_cfg()
MX_APIKEY = _cfg.get('mx_apikey', '') or os.getenv('MX_APIKEY', '')
MX_BASE = 'https://mkapi2.dfcfs.com/finskillshub'

KLINE_DB = '/home/ubuntu/.stock_cache/daily_kline_v1/kline.sqlite'  # 本地K线库
OUTPUT = '/home/ubuntu/projects/mx-trader-bridge/candidates.json'

# 策略参数（v3：从 params.py 中央表读取，不再本地定义）
import params as P
def _A(): return P.STRATEGY_A
def _B(): return P.STRATEGY_B
def _C(): return P.STRATEGY_C


def _xuangu(query: str, max_results: int = 30) -> List[Dict]:
    """调妙想智能选股 API（v3: /api/claw/stock-screen）
    
    返回的 dict 统一包含 secCode/secName/price/chgPct 等标准化字段。
    """
    try:
        r = requests.post(
            f'{MX_BASE}/api/claw/stock-screen',
            headers={'apikey': MX_APIKEY, 'Content-Type': 'application/json'},
            json={'keyword': query},
            timeout=30
        )
        body = r.json()
        inner = body.get('data', {}).get('data', {})
        
        # 1) 先试 allResults JSON（结构化，但有时为空）
        all_res = inner.get('allResults', {}).get('result', {})
        dto_list = all_res.get('dataTableDTOList', [])
        if dto_list:
            raw_list = dto_list[0].get('dataList', [])
            if raw_list:
                return _normalize_fields(raw_list)
        
        # 2) 降级到 partialResults markdown 表格
        pr = inner.get('partialResults', '')
        if pr:
            rows = _parse_partial_results(pr)
            if rows:
                return _normalize_fields(rows)
        
        # 3) 连 partialResults 也没有
        print(f'[picker] xuangu empty: securityCount={inner.get("securityCount", "?")}')
        return []
    except Exception as e:
        print(f'[picker] xuangu error: {e}')
        return []


# ── 旧字段 → 新字段映射 ──────────────────────────────
# allResults JSON key → 标准化 key
_ALL_FIELD_MAP = {
    'SECURITY_CODE': 'secCode',
    'SECURITY_SHORT_NAME': 'secName',
    'NEWEST_PRICE': 'price',
    'CHG': 'chgPct',
    '010000_LIANGBI<70>': 'volRatio',
    '010000_TURNOVER_RATE<70>': 'turnoverRate',
    '010000_FLOWZLAMOUNT<70>': 'mainNetIn',
    '010000_CIRCULATION_MARKET_VALUE<70>': 'flowMarketCap',
    '010000_MARKET_CAPITALIZATION<70>': 'totalMarketCap',
}
# partialResults 中文列头 → 标准化 key
_PR_FIELD_MAP = {
    '代码': 'secCode',
    '名称': 'secName',
    '最新价': 'price',
    '涨跌幅': 'chgPct',
    '换手率': 'turnoverRate',
    '量比': 'volRatio',
    '流通市值': 'flowMarketCap',
    '总市值': 'totalMarketCap',
    '成交额': 'amount',
    '成交量': 'volume',
    '主力净额': 'mainNetIn',        # 主力净额(元)(2026.06.17)
    '主力净流入': 'mainNetIn',       # 别名
    '涨停': 'zt_status',            # 涨停(2026.06.16)
    '首板': 'first_board',          # 首板(2026.06.16)
    '涨停首次封板时间': 'zt_seal_time',
    '涨停封单额': 'zt_seal_amount',
    '涨停封单量': 'zt_seal_qty',
    '概念': 'concepts',
}


def _normalize_fields(items: List[dict]) -> List[dict]:
    """给每个 item 补上标准化字段（secCode / secName / price / chgPct …）"""
    out = []
    for item in items:
        d = dict(item)
        # 尝试从各种可能的 key 提取标准化字段
        _try_map(d, _ALL_FIELD_MAP)
        _try_map(d, _PR_FIELD_MAP)
        # 兜底：如果 price 还没拿到，试试 known 别名
        if 'price' not in d or not d.get('price'):
            for alias in ('latestPrice', 'newestPrice', '最新价', 'NEWEST_PRICE'):
                v = d.get(alias)
                if v:
                    try:
                        d['price'] = float(str(v).replace(',', ''))
                    except (ValueError, TypeError):
                        d['price'] = 0
                    break
        # secCode 补齐 6 位
        code = d.get('secCode', '')
        if code and len(code) < 6:
            d['secCode'] = code.zfill(6)
        out.append(d)
    return out


def _try_map(d: dict, mapping: dict):
    """尝试从 d 的 key 映射到标准化字段"""
    for raw_key, standard_key in mapping.items():
        if standard_key in d:
            continue  # 已有，跳过
        # 精确匹配
        if raw_key in d:
            d[standard_key] = d[raw_key]
            continue
        # 前缀匹配（处理 date-stamped keys 如 010000_LIANGBI<70>{2026-06-17}）
        for k in list(d.keys()):
            if k.startswith(raw_key):
                val = d[k]
                # 清理数值中的中文单位
                d[standard_key] = _parse_value(val)
                break


def _parse_value(val) -> float:
    """去除中文单位，转 float"""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        if '亿' in s:
            return float(s.replace('亿', '').replace(',', '')) * 1e8
        if '万' in s:
            return float(s.replace('万', '').replace(',', '')) * 1e4
        return float(s.replace(',', '').replace('倍', '').replace('%', '').replace('元', ''))
    except (ValueError, TypeError):
        return 0.0


def _parse_partial_results(md: str) -> List[dict]:
    """解析 partialResults markdown 表格 → list[dict]"""
    lines = [l.strip() for l in md.strip().split('\n') if l.strip()]
    if len(lines) < 3:
        return []
    # 第1行 = header；第2行 = 分隔线；第3行起 = 数据
    header = _split_pipe(lines[0])
    if not header:
        return []
    rows = []
    for line in lines[2:]:
        cols = _split_pipe(line)
        if len(cols) < len(header):
            continue
        row = {}
        for i, h in enumerate(header):
            row[h] = cols[i] if i < len(cols) else ''
        rows.append(row)
    return rows


def _split_pipe(line: str) -> List[str]:
    """拆分 markdown 表格行（处理转义 pipe 等）"""
    parts = line.split('|')
    # 去掉首尾空字符串（markdown 行首尾有 |）
    if parts and parts[0] == '':
        parts = parts[1:]
    if parts and parts[-1] == '':
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _kline(code: str, days: int = 30) -> list:
    """从本地 SQLite 取近 N 天日线"""
    try:
        conn = sqlite3.connect(KLINE_DB)
        cur = conn.cursor()
        cur.execute(
            'SELECT date, open, high, low, close, volume FROM kline '
            'WHERE code=? ORDER BY date DESC LIMIT ?', (code, days)
        )
        rows = cur.fetchall()
        conn.close()
        return list(reversed(rows))
    except Exception as e:
        print(f'[picker] kline error {code}: {e}')
        return []


def _ma(closes: list, n: int) -> float:
    if len(closes) < n:
        return 0
    return sum(closes[-n:]) / n


def strategy_a_pullback() -> List[Dict]:
    """A. 强势股回调"""
    query = (
        '近20个交易日涨幅大于15%，'
        '当前价格在5日均线上方，'
        '今日成交量小于昨日，'
        '今日收盘价距MA10均线偏离不超过3%，'
        '非ST，流通市值大于20亿'
    )
    raw = _xuangu(query, 30)
    picks = []
    for s in raw[:_A()['max_picks'] * 3]:
        code = s.get('secCode') or s.get('code', '')
        if not code:
            continue
        kl = _kline(code, 25)
        if len(kl) < 21:
            continue
        closes = [r[4] for r in kl]
        gain = (closes[-1] - closes[-21]) / closes[-21] * 100
        ma5 = _ma(closes, 5)
        ma10 = _ma(closes, 10)
        if gain < _A()['min_gain_pct'] or closes[-1] < ma5:
            continue
        # 回调买点：现价 ≈ MA10
        target = ma10
        picks.append({
            'code': code,
            'name': s.get('secName', s.get('name', '')),
            'strategy': 'A_pullback',
            'conf': min(0.7, gain / 30),
            'target_buy_price': round(target, 2),
            'sl_pct': -3.0,
            'tp_pct': 6.0,
            'reason': f'20日+{gain:.1f}% 现价{closes[-1]:.2f} 回踩MA10={ma10:.2f}',
        })
        if len(picks) >= _A()['max_picks']:
            break
    return picks


def strategy_b_first_zt() -> List[Dict]:
    """B. 首板跟进"""
    query = (
        '昨日涨停，'
        '非ST，'
        '非次新（上市超过1年），'
        '流通市值30亿到150亿，'
        '涨停封单额降序'
    )
    raw = _xuangu(query, 20)
    picks = []
    for s in raw[:_B()['max_picks'] * 3]:
        code = s.get('secCode') or s.get('code', '')
        if not code:
            continue
        # 只取首板（排除连板）
        fb = s.get('first_board', '')
        if fb and '首板' not in str(fb):
            continue
        # 流通市值过滤
        cap = s.get('flowMarketCap', s.get('totalMarketCap', 0))
        if cap and (cap < _B()['market_cap_min'] * 1e8 or
                    cap > _B()['market_cap_max'] * 1e8):
            continue
        kl = _kline(code, 5)
        if len(kl) < 2:
            continue
        yesterday_close = kl[-1][4]
        target = yesterday_close * 1.005  # 昨收+0.5% 内挂单
        picks.append({
            'code': code,
            'name': s.get('secName', s.get('name', '')),
            'strategy': 'B_first_zt',
            'conf': 0.55,
            'target_buy_price': round(target, 2),
            'sl_pct': -3.0,
            'tp_pct': 5.0,
            'reason': f'首板+昨收{yesterday_close:.2f} 流通市值{cap/1e8:.1f}亿',
        })
        if len(picks) >= _B()['max_picks']:
            break
    return picks


def strategy_c_main_inflow() -> List[Dict]:
    """C. 主力净流入"""
    query = (
        f"今日主力净流入大于{_C()['main_inflow_min']}万元，"
        f"量比大于{_C()['volume_ratio_min']}，"
        '今日涨幅2%-7%之间（避免追高），'
        '非ST，'
        '流通市值大于30亿，'
        '主力资金净流入额降序'
    )
    raw = _xuangu(query, 15)
    picks = []
    for s in raw[:_C()['max_picks'] * 3]:
        code = s.get('secCode') or s.get('code', '')
        if not code:
            continue
        price = s.get('price', s.get('latestPrice', 0))
        if not price:
            continue
        # 提取主力净额用于 reason
        mn = s.get('mainNetIn', 0)
        vr = s.get('volRatio', 0)
        target = price  # 现价跟进
        picks.append({
            'code': code,
            'name': s.get('secName', s.get('name', '')),
            'strategy': 'C_main_inflow',
            'conf': 0.50,
            'target_buy_price': round(price, 2),
            'sl_pct': -2.5,
            'tp_pct': 5.0,
            'reason': f'主力净入{mn/1e8:.1f}亿 量比{vr:.1f}',
        })
        if len(picks) >= _C()['max_picks']:
            break
    return picks


def pick_all() -> List[Dict]:
    a = strategy_a_pullback()
    b = strategy_b_first_zt()
    c = strategy_c_main_inflow()
    all_picks = a + b + c
    # 去重（同一只股可能同时被多策略选中，保留 conf 最高的）
    seen = {}
    for p in all_picks:
        if p['code'] not in seen or p['conf'] > seen[p['code']]['conf']:
            seen[p['code']] = p
    return list(seen.values())


def main():
    picks = pick_all()
    out = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'gen_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(picks),
        'picks': picks,
        'params': {'A': _A(), 'B': _B(), 'C': _C()},
    }
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'[picker] 生成 {len(picks)} 个候选 → {OUTPUT}')
    for p in picks:
        print(f'  [{p["strategy"]}] {p["code"]} {p["name"]} '
              f'目标价{p["target_buy_price"]} sl={p["sl_pct"]}% tp={p["tp_pct"]}% '
              f'conf={p["conf"]:.2f} | {p["reason"]}')
    return out


if __name__ == '__main__':
    main()
