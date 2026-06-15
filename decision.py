"""AI 决策模块：生成 DSL + 风控 + 选股"""
import os, json, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'ark')
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', 'ark-code-latest')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')

def chat(messages, temperature=0.7) -> str:
    """调用 LLM"""
    if not LLM_API_KEY:
        return ""
    r = requests.post(f"{LLM_BASE_URL}/chat/completions",
        headers={'Authorization': f'Bearer {LLM_API_KEY}', 'Content-Type': 'application/json'},
        json={'model': LLM_MODEL, 'messages': messages, 'temperature': temperature},
        timeout=60)
    j = r.json()
    return j['choices'][0]['message']['content']

# ---------- 数据获取（东方财富免费接口） ----------

def get_zt_pool():
    """昨日涨停池（东财）"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    url = f"https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=80&sort=fbt:asc&date={yesterday}"
    try:
        r = requests.get(url, timeout=10).json()
        return [s for s in (r.get('data', {}).get('pool') or [])]
    except: return []

def get_hot_stocks(top_n=30):
    """涨幅榜 top N（沪深A）"""
    url = f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={top_n}&po=1&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f3,f5,f6"
    try:
        r = requests.get(url, timeout=10).json()
        return r.get('data', {}).get('diff') or []
    except: return []

def get_market_overview():
    """大盘指数：上证、深证、创业板"""
    codes = '1.000001,0.399001,0.399006'  # 上证/深证/创业板
    url = f"https://push2.eastmoney.com/api/qt/ulist/get?secids={codes}&fields=f2,f3,f4,f12,f14"
    try:
        r = requests.get(url, timeout=10).json()
        return r.get('data', {}).get('diff') or []
    except: return []

# ---------- AI 生成策略 ----------

SYSTEM_PROMPT = """你是A股资深量化交易员，专注超短线（T+1）。今天将对2个仓位（每仓50%资金）下单，全自动执行。

你的任务：
1. 根据当前市场情绪和涨停板/热股数据，选出 **2 只最有可能明天上涨的股票**
2. 给出每只股票的：买入价、止损价（-3%~-5%）、止盈价（+5%~+8%）、最大持仓时长（1-3天）
3. 给出整体策略名称和简短理由

输出严格的 JSON 格式：
{
  "strategy_name": "策略名",
  "market_view": "大盘观点（1句）",
  "picks": [
    {
      "code": "6位代码",
      "name": "股票名",
      "buy_price_hint": "开盘价/集合竞价/不超过XX元",
      "stop_loss_pct": -0.03,
      "take_profit_pct": 0.06,
      "max_hold_days": 2,
      "reason": "为什么选它（1-2句）"
    }
  ],
  "global_risk": {
    "max_drawdown_pct": -0.05,
    "force_exit_time": "14:50"
  }
}

只输出 JSON，不要 markdown 包裹。"""

def generate_decision():
    """生成今日决策"""
    market = get_market_overview()
    zt = get_zt_pool()[:30]
    hot = get_hot_stocks(30)

    user_msg = f"""【今日市场】
大盘：{json.dumps(market, ensure_ascii=False)}

【昨日涨停池 Top30】
{json.dumps(zt, ensure_ascii=False)[:3000]}

【今日涨幅榜 Top30】
{json.dumps(hot, ensure_ascii=False)[:3000]}

【账户】可用资金 60万/仓 × 2 仓
【交易日】{datetime.now().strftime('%Y-%m-%d')}

请输出今日 2 只精选股票的 JSON 决策。"""

    if not LLM_API_KEY:
        # 无 LLM key 时的兜底策略：选昨日涨停池中流通市值适中的前2只
        print("⚠️  无 LLM_API_KEY，使用兜底策略")
        picks = []
        for s in zt[:5]:
            code = s.get('c') or s.get('code')
            name = s.get('n') or s.get('name')
            if code and name:
                picks.append({
                    'code': code, 'name': name,
                    'buy_price_hint': '开盘价',
                    'stop_loss_pct': -0.04,
                    'take_profit_pct': 0.06,
                    'max_hold_days': 2,
                    'reason': '昨日涨停（兜底）'
                })
            if len(picks) >= 2: break
        return {
            'strategy_name': '兜底-昨日涨停延续',
            'market_view': '无AI判断',
            'picks': picks,
            'global_risk': {'max_drawdown_pct': -0.05, 'force_exit_time': '14:50'},
            'ai_reasoning': '无 LLM key 兜底'
        }

    try:
        resp = chat([
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_msg}
        ], temperature=0.7)
        # 清理 markdown
        resp_clean = resp.strip()
        if resp_clean.startswith('```'):
            resp_clean = '\n'.join(resp_clean.split('\n')[1:-1])
        decision = json.loads(resp_clean)
        decision['ai_reasoning'] = resp[:500]
        return decision
    except Exception as e:
        print(f"❌ AI 解析失败: {e}, 返回原文：{resp[:300] if 'resp' in dir() else ''}")
        return None


if __name__ == '__main__':
    d = generate_decision()
    print(json.dumps(d, ensure_ascii=False, indent=2))
