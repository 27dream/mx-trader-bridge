"""AI 决策模块：生成 DSL + 风控 + 选股"""
import os, json, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from config_store import load as _load_cfg
_cfg = _load_cfg()

LLM_PROVIDER = os.getenv('LLM_PROVIDER') or _cfg.get('llm_provider', 'ark')
LLM_API_KEY  = os.getenv('LLM_API_KEY')  or _cfg.get('llm_api_key', '')
LLM_MODEL    = os.getenv('LLM_MODEL')    or _cfg.get('llm_model', 'ark-code-latest')
LLM_BASE_URL = os.getenv('LLM_BASE_URL') or _cfg.get('llm_base_url', 'https://ark.cn-beijing.volces.com/api/v3')
MX_APIKEY    = os.getenv('MX_APIKEY')    or _cfg.get('mx_apikey', '')
MX_API_URL   = os.getenv('MX_API_URL')   or _cfg.get('mx_api_url', 'https://mkapi2.dfcfs.com/finskillshub')

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

# ---------- 数据获取（妙想 stock-screen API） ----------

def mx_screen(keyword: str, page_size: int = 80):
    """调用妙想 stock-screen 真实选股，返回候选列表"""
    if not MX_APIKEY:
        print("⚠️  无 MX_APIKEY，无法选股")
        return []
    try:
        r = requests.post(
            f"{MX_API_URL}/api/claw/stock-screen",
            headers={'apikey': MX_APIKEY, 'Content-Type': 'application/json'},
            json={'keyword': keyword},
            timeout=30,
        )
        j = r.json()
        rows = (((j.get('data') or {}).get('data') or {})
                .get('allResults') or {}).get('result', {}).get('dataList') or []
        # 标准化字段：code / name / price / chg / amount
        out = []
        for it in rows[:page_size]:
            out.append({
                'code': it.get('SECURITY_CODE') or it.get('SECUCODE', '')[:6],
                'name': it.get('SECURITY_SHORT_NAME', ''),
                'price': float(it.get('NEWEST_PRICE') or 0),
                'chg': float(it.get('CHG') or 0),
                'amount': float(it.get('DEAL_AMOUNT') or 0),
            })
        return [x for x in out if x['code']]
    except Exception as e:
        print(f"⚠️  mx_screen 失败: {e}")
        return []

def get_zt_pool():
    """昨日涨停池（妙想真实选股）"""
    return mx_screen("昨日涨停 今日未一字开 流通市值30-300亿")

def get_hot_stocks(top_n=30):
    """今日强势股 top N（妙想真实选股）"""
    return mx_screen(f"今日涨幅3-7% 成交额>5亿 流通市值50-500亿 非ST", page_size=top_n)

def get_market_overview():
    """大盘指数：上证、深证、创业板"""
    codes = '1.000001,0.399001,0.399006'  # 上证/深证/创业板
    url = f"https://push2.eastmoney.com/api/qt/ulist/get?secids={codes}&fields=f2,f3,f4,f12,f14"
    try:
        r = requests.get(url, timeout=10).json()
        return r.get('data', {}).get('diff') or []
    except: return []

# ---------- AI 生成策略 ----------

SYSTEM_PROMPT = """你是A股资深量化交易员，专注超短线策略，全自动执行。今天对2个仓位（每仓50%资金）下单。

【A股核心交易规则 — 必须严格遵守】
1. **T+1**：当日买入的股票次日才可卖，故选股要看次日及之后2-3日的预期，不指望日内套利
2. **涨跌停板**：
   - 主板（60/00/000开头）：±10%
   - 创业板（300/301）/ 科创板（688）：±20%
   - ST/*ST：±5%
   - **绝对不要选今日已涨停的股票**（封板买不进，开板风险大；昨日涨停今日开盘竞价后再判断）
3. **最小交易单位**：100股整数倍，单笔最低100股
4. **交易时间**：9:15-9:25集合竞价 | 9:30-11:30 + 13:00-15:00 连续竞价 | 14:57-15:00 收盘集合竞价
5. **禁选**：ST/*ST 股、停牌股、退市风险股、新股上市首日（涨跌幅±44%规则复杂）
6. **一字板/连板天梯**：连续涨停3天以上的"高位股"次日炸板风险高，新手避开（除非明确是主升浪龙头）
7. **价格规则**：限价单价格须在前收盘价±10%(主板)/20%(创科)区间内，否则废单

【今日策略】
1. 优先候选：**昨日涨停今日未开板/小幅高开** + **早盘放量首板** + **强势板块次新主升**
2. 给出每只股票：买入价提示、止损价（-3%~-5%）、止盈价（+5%~+8%）、最大持仓时长（1-3天）
3. 给出整体策略名称和简短理由

【输出严格JSON】
{
  "strategy_name": "策略名",
  "market_view": "大盘观点（1句）",
  "picks": [
    {
      "code": "6位代码",
      "name": "股票名",
      "board": "main/chinext/star/bj",
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

只输出 JSON，不要 markdown 包裹。注意：T+1 当日买入次日才可卖，故止损/止盈触发最早是次日。"""

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
