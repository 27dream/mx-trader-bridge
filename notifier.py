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


# ============================================================
# PDF 推送：markdown → 美化 HTML → wkhtmltopdf → 企微 file 消息
# ============================================================
_PDF_CSS = """<style>
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:780px;margin:40px auto;padding:0 20px;line-height:1.7;color:#222}
h1{border-bottom:3px solid #2563eb;padding-bottom:8px;color:#1e40af}
h2{border-left:4px solid #2563eb;padding-left:10px;margin-top:30px;color:#1e3a8a}
h3{color:#1d4ed8}
table{border-collapse:collapse;margin:12px 0;width:100%}
th,td{border:1px solid #cbd5e1;padding:8px 12px}
th{background:#eff6ff}
code{background:#f1f5f9;padding:2px 6px;border-radius:4px;color:#be123c;font-family:Menlo,Consolas,monospace}
pre{background:#0f172a;color:#e2e8f0;padding:16px;border-radius:8px;overflow-x:auto;line-height:1.5}
pre code{background:transparent;color:inherit;padding:0}
blockquote{border-left:4px solid #94a3b8;background:#f8fafc;padding:8px 16px;color:#475569;margin:16px 0}
hr{border:none;border-top:1px dashed #cbd5e1;margin:24px 0}
ul,ol{padding-left:24px}
li{margin:4px 0}
.profit-pos{color:#dc2626;font-weight:bold}
.profit-neg{color:#16a34a;font-weight:bold}
</style>"""


def _md_to_pdf(content: str, output_pdf: str, is_markdown: bool = True) -> bool:
    """渲染 markdown/HTML 字符串为 PDF。返回是否成功。"""
    import subprocess, tempfile
    try:
        if is_markdown:
            try:
                import markdown
            except ImportError:
                print("  notifier.pdf: 需要 pip install markdown")
                return False
            html_body = markdown.markdown(content, extensions=['tables', 'fenced_code', 'nl2br'])
        else:
            html_body = content
        full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'>{_PDF_CSS}</head><body>{html_body}</body></html>"
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(full_html)
            html_path = f.name
        r = subprocess.run(
            ['wkhtmltopdf', '--encoding', 'utf-8', '--enable-local-file-access',
             '--quiet', html_path, output_pdf],
            capture_output=True, text=True, timeout=30,
        )
        os.unlink(html_path)
        if r.returncode != 0 or not os.path.exists(output_pdf):
            print(f"  notifier.pdf: wkhtmltopdf 失败 {r.stderr[:200]}")
            return False
        return True
    except FileNotFoundError:
        print("  notifier.pdf: 系统未装 wkhtmltopdf（apt install wkhtmltopdf）")
        return False
    except Exception as e:
        print(f"  notifier.pdf: 渲染异常 {e}")
        return False


def _wecom_upload_file(file_path: str) -> str:
    """上传文件到企微群机器人，返回 media_id（3 天内有效）。"""
    if not WECOM_BOT_WEBHOOK or not os.path.exists(file_path):
        return ''
    # 从 webhook URL 取 key
    import re
    m = re.search(r'key=([\w-]+)', WECOM_BOT_WEBHOOK)
    if not m:
        return ''
    upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={m.group(1)}&type=file"
    try:
        with open(file_path, 'rb') as f:
            r = requests.post(upload_url, files={'media': f}, timeout=30)
        body = r.json()
        if body.get('errcode') == 0:
            return body.get('media_id', '')
        print(f"  notifier.wecom_upload: {body}")
    except Exception as e:
        print(f"  notifier.wecom_upload: {e}")
    return ''


def _wecom_send_file(media_id: str) -> bool:
    """发 file 消息到企微群。"""
    if not WECOM_BOT_WEBHOOK or not media_id:
        return False
    try:
        r = requests.post(
            WECOM_BOT_WEBHOOK,
            json={'msgtype': 'file', 'file': {'media_id': media_id}},
            timeout=10,
        )
        return r.json().get('errcode') == 0
    except Exception as e:
        print(f"  notifier.wecom_send_file: {e}")
        return False


def notify_pdf(content: str, filename: str = None, is_markdown: bool = True,
               keep_pdf: bool = False) -> dict:
    """把 Markdown/HTML 内容渲染成 PDF 推送到企业微信群。

    Args:
        content: Markdown 字符串 或 HTML 字符串 或 .md 文件路径
        filename: 输出 PDF 文件名（不含路径），不指定则用时间戳
        is_markdown: True=按 markdown 渲染，False=按 HTML 渲染
        keep_pdf: True=保留生成的 PDF 文件，False=发完就删

    Returns:
        {ok: bool, pdf_path: str, media_id: str}
    """
    import tempfile

    # 内容是文件路径？直接读
    if is_markdown and os.path.exists(content) and content.endswith('.md'):
        with open(content, 'r', encoding='utf-8') as f:
            content = f.read()

    # 决定输出路径
    if not filename:
        filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    if not filename.endswith('.pdf'):
        filename += '.pdf'
    pdf_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'pdf')
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, filename)

    out = {'ok': False, 'pdf_path': pdf_path, 'media_id': ''}

    # 渲染
    if not _md_to_pdf(content, pdf_path, is_markdown):
        return out

    print(f"  📄 PDF 已生成 {pdf_path} ({os.path.getsize(pdf_path)//1024}KB)")

    # 上传 + 发送
    media_id = _wecom_upload_file(pdf_path)
    if not media_id:
        return out
    out['media_id'] = media_id
    out['ok'] = _wecom_send_file(media_id)

    if not keep_pdf and out['ok']:
        try: os.unlink(pdf_path)
        except: pass

    return out


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
