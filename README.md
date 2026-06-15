# 🚀 MX Trader Bridge

> **stockgpt-review 决策大脑 → 东方财富妙想模拟盘自动执行**的桥接层
> A股全自动模拟交易系统：AI 选股 + AI 风控 + 自动下单 + 每日复盘 + 周度反思迭代

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Stars](https://img.shields.io/github/stars/27dream/mx-trader-bridge?style=social)](https://github.com/27dream/mx-trader-bridge)

## 📸 控制台预览

![MX Trader Bridge Console](./assets/console.png)

> 一个本地 Flask 面板（端口 8787），BYOK 配置 5 大主流 LLM，一键测试 / 保存 / 手动触发交易流程

## ✨ 特性

- 🤖 **BYOK 多 LLM 支持** — 字节豆包 ARK / DeepSeek / Kimi / 通义千问 / 智谱 GLM / OpenAI / 自定义 7 模板，Web 面板自助切换
- 📊 **2 仓位 × 50%** 默认配置，AI **动态生成**止损/止盈/强平时间参数（不是写死的 -3%）
- 🎯 **腾讯实时行情** 接入（盘中 0 延迟）
- 🔁 **周日 AI 反思** — 自动复盘本周战绩，进化下周策略 DSL
- 💾 **SQLite 战绩库** — trades / decisions / recaps / reflections 四表完整可查
- 📱 **企业微信日报推送** — Webhook 可选
- 🔒 凭证 chmod 600 存 `~/.mx-trader-bridge/config.json`，**不入仓库**
- ⚡ **0 成本运行** — 全本地 cron，无服务器，无云费用

## 🚀 快速开始

```bash
git clone https://github.com/27dream/mx-trader-bridge
cd mx-trader-bridge
pip install -r requirements.txt
python server.py   # 浏览器打开 http://localhost:8787
```

打开面板 → 填妙想 API Key + LLM Key → 点「测试连接」→ 保存 → 完事。

## 🏗️ 架构

```
            ┌─────────────────────────┐
            │ stockgpt-review (Vercel)│  AI 决策大脑（云端展示）
            └──────────┬──────────────┘
                       │ HTTP
            ┌──────────▼──────────────┐
            │   decision.py            │  09:25 出选股 + 风控 DSL
            └──────────┬──────────────┘
                       │
            ┌──────────▼──────────────┐
            │ morning_trade.py         │  09:30 建仓
            ├──────────────────────────┤
            │ monitor.py               │  盘中每 5min 盯盘
            ├──────────────────────────┤
            │ recap.py                 │  15:30 复盘 + 微信推送
            ├──────────────────────────┤
            │ reflect.py               │  周日 20:00 AI 反思
            └──────────┬──────────────┘
                       │ mx-moni API
            ┌──────────▼──────────────┐
            │  东方财富妙想模拟盘       │  120 万本金练手
            └─────────────────────────┘
```

## 📂 文件结构

| 文件 | 作用 |
|---|---|
| `server.py` | Flask 控制面板（端口 8787） |
| `templates/index.html` | BYOK 配置 UI |
| `trader.py` | 妙想 mx-moni API 封装（买入/卖出/查持仓） |
| `decision.py` | AI 决策（调 stockgpt + LLM 出选股 DSL） |
| `morning_trade.py` | 09:30 建仓主流程 |
| `monitor.py` | 盘中盯盘 + 止损/止盈触发 |
| `recap.py` | 15:30 复盘 + 企业微信日报 |
| `reflect.py` | 周日 AI 反思迭代策略 |
| `db.py` | SQLite 数据层 |
| `llm_templates.py` | 7 LLM 模板定义 |
| `config_store.py` | 凭证 chmod 600 安全存储 |
| `cron.txt` | Cron 调度规则 |

## 📅 Cron 调度

```
25 9   * * 1-5  python decision.py        # 09:25 出决策
30 9   * * 1-5  python morning_trade.py   # 09:30 建仓
*/5 9-14 * * 1-5 python monitor.py        # 盘中每 5min 盯盘
30 15  * * 1-5  python recap.py           # 15:30 复盘 + 推送
0  20  * * 0    python reflect.py         # 周日 20:00 AI 反思
```

`crontab -e` 粘贴 `cron.txt` 内容即可。

## 🤖 支持的 LLM

| 提供商 | 默认模型 | 注册地址 |
|---|---|---|
| 🚀 字节豆包 ARK | `doubao-seed-1.6` | https://www.volcengine.com/product/ark |
| 🐬 DeepSeek | `deepseek-chat` | https://platform.deepseek.com |
| 🌙 月之暗面 Kimi | `moonshot-v1-8k` | https://platform.moonshot.cn |
| 🧠 阿里通义千问 | `qwen-plus` | https://dashscope.aliyun.com |
| 🔮 智谱 GLM | `glm-4-flash` | https://open.bigmodel.cn |
| 🤖 OpenAI | `gpt-4o-mini` | https://platform.openai.com |
| ⚙️ 自定义 | OpenAI 兼容 | 任意网关 |

## ⚠️ 风险声明

**仅用于东方财富妙想模拟盘**学习研究，不操作真实资金，不构成任何投资建议。
A股有风险，量化需谨慎。

## 📜 License

MIT — 随便玩，star 一下回血 ⭐

## 🔗 相关项目

- [stockgpt-review](https://github.com/27dream/stockgpt-review) — 配套的 AI 决策大脑（Vercel 部署）
- [mcp-eastmoney](https://github.com/27dream/mcp-eastmoney) — 东方财富 MCP Server
