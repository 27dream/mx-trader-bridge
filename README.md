# 🚀 MX Trader Bridge

**stockgpt-review 决策大脑 → 妙想模拟盘自动执行**的桥接层。

A股全自动模拟交易系统：AI 选股 + AI 风控 + 自动下单 + 每日复盘 + 周度反思迭代。

## ✨ 特性

- 🤖 **BYOK 多 LLM 支持** — ARK / DeepSeek / Kimi / Qwen / OpenAI 五模板，Web 面板配置
- 📊 **2 仓位 × 50%** 默认配置，AI 动态生成止损/止盈参数
- 🎯 **腾讯实时行情** 接入（盘中无延迟）
- 🔁 **周日 AI 反思** — 自动复盘战绩，优化下周策略 DSL
- 💾 **SQLite 战绩库** — trades/decisions/recaps/reflections 四表
- 📱 **微信日报推送** — Webhook 可选
- 🔒 凭证 chmod 600 存于 `~/.mx-trader-bridge/config.json`

## 🚀 快速开始

```bash
git clone <repo> && cd mx-trader-bridge
pip install -r requirements.txt
cp .env.example .env  # 编辑或用 Web 面板配置
python web_panel.py   # 访问 http://localhost:8787
```

## 🏗️ 架构

```
stockgpt-review (Vercel) ──HTTP──┐
                                  ↓
        AI 决策 ←── decision.py ──┤
                                  ↓
   妙想模拟盘 ←── trader.py ←── morning_trade.py (09:30 建仓)
                                  ↓
                              monitor.py (盘中盯盘)
                                  ↓
                              recap.py (15:30 复盘)
                                  ↓
                              reflect.py (周日反思)
```

## 📂 文件

| 文件 | 作用 |
|---|---|
| `trader.py` | 妙想 mx-moni API 封装 |
| `decision.py` | AI 决策（调 stockgpt + LLM 出 DSL） |
| `morning_trade.py` | 09:30 建仓主流程 |
| `monitor.py` | 盘中盯盘 + 风控触发 |
| `recap.py` | 15:30 复盘 + 微信日报 |
| `reflect.py` | 周日 AI 反思迭代 |
| `web_panel.py` | Flask 控制面板（端口 8787） |
| `db.py` | SQLite 数据层 |
| `llm_templates.py` | 5 LLM 模板定义 |

## 📅 Cron

见 `cron.txt`，`crontab -e` 粘贴即可。

## ⚠️ 风险声明

仅用于妙想**模拟盘**学习研究，不构成投资建议。

## 📜 License

MIT
