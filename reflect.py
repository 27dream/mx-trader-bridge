"""周日 AI 反思 — 复盘本周战绩 + 优化下周策略 DSL"""
import os, json, sqlite3
from datetime import datetime, timedelta
from openai import OpenAI
from db import get_conn
from config_store import load_config

def weekly_reflect():
    cfg = load_config()
    conn = get_conn()
    cur = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    trades = cur.execute("SELECT * FROM trades WHERE date>=? ORDER BY date", (week_ago,)).fetchall()
    recaps = cur.execute("SELECT * FROM recaps WHERE date>=? ORDER BY date", (week_ago,)).fetchall()

    if not trades and not recaps:
        return "本周无交易数据，跳过反思"

    summary = {
        "trades": [dict(t) for t in trades],
        "recaps": [dict(r) for r in recaps],
        "win_rate": sum(1 for t in trades if (t['profit'] or 0) > 0) / max(len(trades), 1),
    }

    client = OpenAI(api_key=cfg.get('llm_api_key'), base_url=cfg.get('llm_base_url'))
    prompt = f"""你是A股量化策略迭代专家。分析本周战绩，输出 JSON：
- weak_points: 本周策略弱点(数组)
- next_dsl: 下周选股 DSL 调整建议(JSON)
- risk_adjust: 止损/止盈参数调整建议
战绩数据: {json.dumps(summary, ensure_ascii=False, default=str)[:3000]}"""

    resp = client.chat.completions.create(
        model=cfg.get('llm_model'),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    reflection = resp.choices[0].message.content
    cur.execute("INSERT INTO reflections(date,content,win_rate) VALUES(?,?,?)",
                (datetime.now().strftime('%Y-%m-%d'), reflection, summary['win_rate']))
    conn.commit()
    return reflection

if __name__ == "__main__":
    print(weekly_reflect())
