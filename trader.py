"""核心交易模块：调用妙想 mx-moni API"""
import os, json, requests
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

MX_APIKEY = os.getenv('MX_APIKEY')
MX_API_URL = os.getenv('MX_API_URL', 'https://mkapi2.dfcfs.com/finskillshub')

def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{MX_API_URL}{path}",
        headers={'apikey': MX_APIKEY, 'Content-Type': 'application/json'},
        json=body, timeout=15)
    return r.json()

def get_balance() -> dict:
    """查资金 → {totalAssets, availBalance, totalPosPct}"""
    return _post('/api/claw/mockTrading/balance', {'moneyUnit': 1})

def get_positions() -> list:
    """查持仓 → [{secCode, secName, count, availCount, costPrice, price, profitPct, ...}]"""
    res = _post('/api/claw/mockTrading/positions', {'moneyUnit': 1})
    pos_list = res.get('data', {}).get('posList') or []
    # 还原价格
    for p in pos_list:
        if p.get('priceDec'):
            p['_price'] = p['price'] / (10 ** p['priceDec'])
        if p.get('costPriceDec'):
            p['_costPrice'] = p['costPrice'] / (10 ** p['costPriceDec'])
    return pos_list

def get_orders(status: int = 0) -> list:
    """查委托：status=0全部, 2已报, 4已成"""
    res = _post('/api/claw/mockTrading/orders', {'fltOrderDrt': 0, 'fltOrderStatus': status})
    return res.get('data', {}).get('orders') or []

def buy(stock_code: str, quantity: int, price: Optional[float] = None) -> dict:
    """买入：price=None 则市价单"""
    body = {'type': 'buy', 'stockCode': stock_code, 'quantity': quantity}
    if price is None:
        body['useMarketPrice'] = True
        body['price'] = 0
    else:
        body['useMarketPrice'] = False
        body['price'] = round(price, 2)
    return _post('/api/claw/mockTrading/trade', body)

def sell(stock_code: str, quantity: int, price: Optional[float] = None) -> dict:
    """卖出：price=None 则市价单"""
    body = {'type': 'sell', 'stockCode': stock_code, 'quantity': quantity}
    if price is None:
        body['useMarketPrice'] = True
        body['price'] = 0
    else:
        body['useMarketPrice'] = False
        body['price'] = round(price, 2)
    return _post('/api/claw/mockTrading/trade', body)

def cancel_all() -> dict:
    """一键撤单"""
    return _post('/api/claw/mockTrading/cancel', {'type': 'all'})

def cancel_order(order_id: str, stock_code: str) -> dict:
    return _post('/api/claw/mockTrading/cancel',
        {'type': 'order', 'orderId': order_id, 'stockCode': stock_code})

def post_diary(html: str) -> dict:
    """发经验交流帖"""
    return _post('/api/claw/mockTrading/newPost', {'text': html})


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'balance'
    if cmd == 'balance':
        print(json.dumps(get_balance(), ensure_ascii=False, indent=2))
    elif cmd == 'positions':
        print(json.dumps(get_positions(), ensure_ascii=False, indent=2))
    elif cmd == 'orders':
        print(json.dumps(get_orders(), ensure_ascii=False, indent=2))
