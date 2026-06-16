"""统一告警通道：企业微信群机器人 / 飞书 webhook / Server酱 / 控制台

环境变量（.env 任选其一即可，不配则只打印控制台）：
    WECOM_BOT_WEBHOOK — 企业微信群机器人 webhook（推荐 ✅）
    FEISHU_WEBHOOK    — 飞书自定义机器人 webhook
    SERVERCHAN_KEY    — Server酱 SCT 密钥

用法：
    from notifier import notify, alert
    notify("📈 已建仓 002015 协鑫能科 ¥7.21 × 8000")
    alert("🚨 风控熔断：单日亏损达到 -3%", level="critical")
"""
import os, json, time, requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

WECOM_BOT_WEBHOOK = os.getenv('WECOM_BOT_WEBHOOK', '').strip()
FEISHU_WEBHOOK = os.getenv('FEISHU_WEBHOOK', '').strip()
SERVERCHAN_KEY = os.getenv('SERVERCHAN_KEY', '').strip()

LEVEL_PREFIX = {
    'info':     'ℹ️ ',
    'warn':     '⚠️ ',
    'critical': '🚨 ',
    'success':  '✅ ',
}


def _to_wecom_bot(text: str, level: str = 'info', use_markdown: bool = True) -> bool:
    """企业微信群机器人。critical/warn 用 markdown 染色，info/success 用纯文本。"""
    if not WECOM_BOT_WEBHOOK:
        return False
    try:
        if use_markdown and level in ('critical', 'warn'):
            color = 'warning' if level == 'warn' else 'info'
            # 企业微信只支持 info(灰)/comment(灰)/warning(橙) 三种
            if level == 'critical':
                color = 'warning'  # 红色不支持，用橙色代替
            payload = {
                'msgtype': 'markdown',
                'markdown': {'content': f"<font color=\"{color}\">**[{level.upper()}]**</font>\n{text}"}
            }
        else:
            payload = {'msgtype': 'text', 'text': {'content': text}}
        r = requests.post(WECOM_BOT_WEBHOOK, json=payload, timeout=8)
        return r.json().get('errcode') == 0
    except Exception as e:
        print(f"  notifier.wecom fail: {e}")
        return False


def _to_feishu(text: str, level: str = 'info') -> bool:
    if not FEISHU_WEBHOOK:
        return False
    try:
        r = requests.post(FEISHU_WEBHOOK, json={
            'msg_type': 'text',
            'content': {'text': f"[{level.upper()}] {text}"}
        }, timeout=8)
        body = r.json()
        return body.get('code', body.get('StatusCode')) in (0, 200, '0', '200')
    except Exception as e:
        print(f"  notifier.feishu fail: {e}")
        return False


def _to_serverchan(title: str, text: str) -> bool:
    if not SERVERCHAN_KEY:
        return False
    try:
        r = requests.post(
            f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
            data={'title': title[:32], 'desp': text[:1000]},
            timeout=8,
        )
        return r.json().get('code') == 0
    except Exception as e:
        print(f"  notifier.serverchan fail: {e}")
        return False


def notify(text: str, level: str = 'info', title: str = None) -> dict:
    """发送一条消息到所有已配置通道。

    Returns: {console, wecom, feishu, serverchan} 各通道布尔结果
    """
    prefix = LEVEL_PREFIX.get(level, '')
    full = f"{prefix}{text}"
    ts = datetime.now().strftime('%H:%M:%S')

    # 控制台总是打
    print(f"[{ts}] {full}")

    out = {'console': True, 'wecom': False, 'feishu': False, 'serverchan': False}
    out['wecom'] = _to_wecom_bot(full, level)
    out['feishu'] = _to_feishu(full, level)
    out['serverchan'] = _to_serverchan(title or f"MX-Trader {level}", full)
    return out


def alert(text: str, level: str = 'critical', title: str = None) -> dict:
    """语义同 notify，但默认 critical 级别 — 用于熔断/拒单/重大异常。"""
    return notify(text, level=level, title=title)


# ─── 业务封装（建议各模块用这些，避免散落） ───────────────────

def notify_decision(strategy_name: str, picks: list, market_view: str = ''):
    if not picks:
        return notify(f"📋 决策无标的｜策略={strategy_name}｜大盘={market_view}", level='warn')
    body = '\n'.join([f"  · {p.get('code')} {p.get('name','')} 止损{p.get('stop_loss_pct',0)*100:+.1f}% 止盈{p.get('take_profit_pct',0)*100:+.1f}%"
                      for p in picks])
    return notify(f"🧠 今日决策｜{strategy_name}\n大盘：{market_view}\n标的：\n{body}", level='info', title='今日决策')


def notify_fill(code: str, name: str, action: str, price: float, qty: int, reason: str = ''):
    sign = '🛒 买入' if action.upper() == 'BUY' else '💰 卖出'
    return notify(f"{sign} {code} {name} ¥{price:.2f} × {qty}（{reason}）", level='success', title=f'{action} 成交')


def notify_reject(code: str, name: str, reason: str):
    return alert(f"🚫 下单被拒 {code} {name}：{reason}", level='warn', title='下单被拒')


def notify_circuit_break(reason: str):
    return alert(f"⛔ 风控熔断：{reason}", level='critical', title='风控熔断')


if __name__ == '__main__':
    # 自检
    print("=== notifier 自检 ===")
    print(f"WECOM_BOT_WEBHOOK 配置: {'✅' if WECOM_BOT_WEBHOOK else '❌ (未配置)'}")
    print(f"FEISHU_WEBHOOK 配置: {'✅' if FEISHU_WEBHOOK else '❌ (未配置)'}")
    print(f"SERVERCHAN_KEY 配置: {'✅' if SERVERCHAN_KEY else '❌ (未配置)'}")
    r = notify("notifier 自检：这是一条 info 测试消息", level='info', title='自检')
    print(f"投递结果: {r}")
