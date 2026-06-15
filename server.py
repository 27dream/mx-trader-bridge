"""Web 配置面板 + 控制中心 (端口 8787)"""
import os, json, subprocess, sys
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect
import config_store
from llm_templates import LLM_TEMPLATES

app = Flask(__name__)
ROOT = os.path.dirname(__file__)

def apply_env():
    """把 config 注入环境变量"""
    cfg = config_store.load()
    os.environ['MX_APIKEY'] = cfg['mx_apikey']
    os.environ['MX_API_URL'] = cfg['mx_api_url']
    os.environ['LLM_PROVIDER'] = cfg['llm_provider']
    os.environ['LLM_API_KEY'] = cfg['llm_api_key']
    os.environ['LLM_BASE_URL'] = cfg['llm_base_url']
    os.environ['LLM_MODEL'] = cfg['llm_model']
    os.environ['POSITION_COUNT'] = str(cfg['position_count'])
    os.environ['POSITION_PCT'] = str(cfg['position_pct'])
    os.environ['WECHAT_WEBHOOK'] = cfg['wechat_webhook']

apply_env()

INDEX_HTML = open(os.path.join(ROOT, 'templates', 'index.html')).read() if os.path.exists(os.path.join(ROOT, 'templates', 'index.html')) else ''

@app.route('/')
def index():
    cfg = config_store.load()
    return render_template_string(INDEX_HTML, cfg=cfg, templates=LLM_TEMPLATES)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        cfg = config_store.load()
        # 脱敏
        safe = dict(cfg)
        if safe.get('mx_apikey'): safe['mx_apikey'] = safe['mx_apikey'][:6]+'***'+safe['mx_apikey'][-4:]
        if safe.get('llm_api_key'): safe['llm_api_key'] = safe['llm_api_key'][:6]+'***'+safe['llm_api_key'][-4:]
        return jsonify(safe)
    data = request.json or {}
    cfg = config_store.load()
    for k in ['mx_apikey','mx_api_url','llm_provider','llm_api_key','llm_base_url','llm_model',
              'position_count','position_pct','wechat_webhook','auto_trade_enabled']:
        if k in data and data[k] != '' and not (isinstance(data[k],str) and '***' in data[k]):
            cfg[k] = data[k]
    config_store.save(cfg)
    apply_env()
    return jsonify({'ok': True, 'msg': '✅ 已保存'})

@app.route('/api/templates')
def api_templates():
    return jsonify(LLM_TEMPLATES)

@app.route('/api/test_mx', methods=['POST'])
def test_mx():
    apply_env()
    import trader
    try:
        r = trader.get_balance()
        if r.get('code') == '200':
            d = r['data']
            return jsonify({'ok': True, 'msg': f"✅ {d.get('accName','?')} | 总资产 {d.get('totalAssets',0):,.0f} | 可用 {d.get('availBalance',0):,.0f}"})
        return jsonify({'ok': False, 'msg': f"❌ {r.get('message')}"})
    except Exception as e:
        return jsonify({'ok': False, 'msg': f"❌ {e}"})

@app.route('/api/test_llm', methods=['POST'])
def test_llm():
    apply_env()
    import importlib, decision
    importlib.reload(decision)
    try:
        resp = decision.chat([{'role':'user','content':'回复:OK'}])
        return jsonify({'ok': True, 'msg': f"✅ {resp[:100]}"})
    except Exception as e:
        return jsonify({'ok': False, 'msg': f"❌ {e}"})

@app.route('/api/run/<task>', methods=['POST'])
def run_task(task):
    apply_env()
    scripts = {'morning':'morning_trade.py','monitor':'monitor.py','recap':'recap.py'}
    if task not in scripts: return jsonify({'ok':False,'msg':'未知任务'})
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, scripts[task])],
            capture_output=True, text=True, timeout=120, cwd=ROOT, env=os.environ.copy())
        return jsonify({'ok': r.returncode==0, 'msg': r.stdout[-2000:] + ('\n'+r.stderr[-500:] if r.stderr else '')})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)})

@app.route('/api/status')
def status():
    apply_env()
    import trader, db
    try:
        bal = trader.get_balance().get('data', {})
        pos = trader.get_positions()
        recaps = db.get_recent_recaps(7)
        return jsonify({
            'balance': bal,
            'positions': [{'code':p['secCode'],'name':p['secName'],
                           'count':p.get('count',0),
                           'cost': (p['costPrice']/(10**p.get('costPriceDec',2))) if p.get('costPrice') else 0,
                           'price': (p['price']/(10**p.get('priceDec',2))) if p.get('price') else 0,
                           'profit_pct': p.get('profitPct',0)} for p in pos if p.get('count',0)>0],
            'recaps': recaps,
        })
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    db_path = os.path.join(ROOT, 'db.sqlite')
    if not os.path.exists(db_path):
        import db; db.init_db()
    print("🌐 控制台: http://localhost:8787")
    app.run(host='0.0.0.0', port=8787, debug=False)
