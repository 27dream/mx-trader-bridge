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

KLINE_DB = '/home/ubuntu/data/a_share_kline.db'  # baostock 本地库
OUTPUT = '/home/ubuntu/projects/mx-trader-bridge/candidates.json'

# 策略参数（v3：从 params.py 中央表读取，不再本地定义）
import params as P
def _A(): return P.STRATEGY_A
def _B(): return P.STRATEGY_B
def _C(): return P.STRATEGY_C


def _xuangu(query: str, max_results: int = 30) -> List[Dict]:
    """调妙想智能选股 API"""
    try:
        r = requests.post(
            f'{MX_BASE}/mx-xuangu/select',
            headers={'apikey': MX_APIKEY, 'Content-Type': 'application/json'},
            json={'query': query, 'partialResults': True, 'maxResults': max_results},
            timeout=30
        )
        data = r.json().get('data', {})
        return data.get('partialResults', []) or data.get('allResults', [])
    except Exception as e:
        print(f'[picker] xuangu error: {e}')
        return []


def _kline(code: str, days: int = 30) -> list:
    """从本地 SQLite 取近 N 天日线"""
    try:
        conn = sqlite3.connect(KLINE_DB)
        cur = conn.cursor()
        cur.execute(
            'SELECT date, open, high, low, close, volume FROM daily_kline '
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
        '昨日涨停且为首板（前一日未涨停），'
        '今日开盘竞价不破昨日收盘价的98%，'
        '流通市值在30-150亿之间，'
        '非ST，非次新（上市超过1年）'
    )
    raw = _xuangu(query, 20)
    picks = []
    for s in raw[:_B()['max_picks'] * 3]:
        code = s.get('secCode') or s.get('code', '')
        if not code:
            continue
        cap = s.get('totalMarketCap', s.get('flowMarketCap', 0))
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
        '非ST，流通市值大于30亿'
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
        target = price  # 现价跟进
        picks.append({
            'code': code,
            'name': s.get('secName', s.get('name', '')),
            'strategy': 'C_main_inflow',
            'conf': 0.50,
            'target_buy_price': round(price, 2),
            'sl_pct': -2.5,
            'tp_pct': 5.0,
            'reason': f'主力净入{s.get("mainNetIn", 0)/1e4:.0f}万 量比{s.get("volRatio", 0):.1f}',
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
