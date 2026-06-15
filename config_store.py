"""配置存储：~/.mx-trader-bridge/config.json"""
import os, json
from pathlib import Path

CONFIG_DIR = Path.home() / '.mx-trader-bridge'
CONFIG_FILE = CONFIG_DIR / 'config.json'

DEFAULT = {
    'mx_apikey': '',
    'mx_api_url': 'https://mkapi2.dfcfs.com/finskillshub',
    'llm_provider': 'ark',
    'llm_api_key': '',
    'llm_base_url': 'https://ark.cn-beijing.volces.com/api/v3',
    'llm_model': '',
    'position_count': 2,
    'position_pct': 0.5,
    'wechat_webhook': '',
    'auto_trade_enabled': False,
}

def load():
    CONFIG_DIR.mkdir(exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
            return {**DEFAULT, **d}
        except: pass
    return dict(DEFAULT)

def save(cfg):
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_FILE, 0o600)
    return True

def get(key, default=None):
    return load().get(key, default)
