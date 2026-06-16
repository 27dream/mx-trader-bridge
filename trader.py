"""核心交易模块：调用妙想 mx-moni API"""
import os, json, requests
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
from config_store import load as _load_cfg
_cfg = _load_cfg()

# 账号一致性保障：config 优先（Web面板写入），env 仅兜底，避免 hermes 进程残留旧 env 串账号
MX_APIKEY  = _cfg.get('mx_apikey', '') or os.getenv('MX_APIKEY', '')
MX_API_URL = _cfg.get('mx_api_url', '') or os.getenv('MX_API_URL', 'https://mkapi2.dfcfs.com/finskillshub')

class TradeError(Exception):
    """下单业务失败（rc != 0）"""
    pass

def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{MX_API_URL}{path}",
        headers={'apikey': MX_APIKEY, 'Content-Type': 'application/json'},
        json=body, timeout=15)
    return r.json()

def _trade(body: dict) -> dict:
    """下单 + rc=0 校验。rc!=0 抛 TradeError，含完整 response

    妙想真实响应结构：
      { "code": "200", "data": { "rc": 0, "rmsg": "", "orderID": "261...", "result": {...} } }
    rc 字段在 data.rc（不是 data.result.rc），data.result 只装 status/interval。
    顶层 code != "200" 时（如余额不足）data 可能为 null，统一按失败处理。
    """
    res = _post('/api/claw/mockTrading/trade', body)
    top_code = str(res.get('code', ''))
    data = res.get('data') or {}
    # 顶层非 200（如 501 余额不足）→ 直接失败
    if top_code and top_code != '200':
        raise TradeError(f"下单失败 code={top_code} msg={res.get('message')} body={body} resp={res}")
    # rc 优先取 data.rc，兼容旧字段 data.result.rc
    rc = data.get('rc')
    if rc is None:
        rc = (data.get('result') or {}).get('rc')
    if rc != 0:
        rmsg = data.get('rmsg') or (data.get('result') or {}).get('rmsg') or res.get('message') or 'unknown'
        raise TradeError(f"下单失败 rc={rc} msg={rmsg} body={body} resp={res}")
    return res

def _extract_order_id(trade_resp: dict) -> str:
    """从下单响应抽 orderID（妙想真实字段名是大写 D：data.orderID）"""
    d = trade_resp.get('data') or {}
    return d.get('orderID') or d.get('orderId') or (d.get('result') or {}).get('orderId') or ''

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
    return _trade(body)

def sell(stock_code: str, quantity: int, price: Optional[float] = None) -> dict:
    """卖出：price=None 则市价单"""
    body = {'type': 'sell', 'stockCode': stock_code, 'quantity': quantity}
    if price is None:
        body['useMarketPrice'] = True
        body['price'] = 0
    else:
        body['useMarketPrice'] = False
        body['price'] = round(price, 2)
    return _trade(body)

def cancel_all() -> dict:
    """一键撤单"""
    return _post('/api/claw/mockTrading/cancel', {'type': 'all'})

def cancel_order(order_id: str, stock_code: str) -> dict:
    return _post('/api/claw/mockTrading/cancel',
        {'type': 'order', 'orderId': order_id, 'stockCode': stock_code})

def verify_filled(stock_code: str, quantity: int, drt: int = 1, max_wait: int = 20) -> dict:
    """轮询确认成交。drt=1买/2卖。返回 {filled:bool, status, tradeCount, avgPrice}"""
    import time
    target_drt = drt
    end = time.time() + max_wait
    last = {}
    while time.time() < end:
        try:
            orders = get_orders(0)  # 全部委托
            # 找最近一笔匹配股票+方向的订单
            cands = [o for o in orders
                     if o.get('secCode') == stock_code
                     and o.get('drt') == target_drt]
            if cands:
                # 按时间倒序
                cands.sort(key=lambda x: x.get('time', 0), reverse=True)
                o = cands[0]
                status = o.get('status')
                trade_cnt = o.get('tradeCount', 0)
                price_dec = o.get('priceDec', 2)
                avg_price = (o.get('tradePrice', 0) / (10 ** price_dec)) if trade_cnt else 0
                last = {'status': status, 'tradeCount': trade_cnt,
                        'avgPrice': avg_price, 'orderId': o.get('id') or o.get('orderId')}
                if status == 4 and trade_cnt >= quantity:  # 全部成交
                    last['filled'] = True
                    return last
                if status == 8:  # 已撤
                    last['filled'] = False
                    last['reason'] = 'cancelled'
                    return last
        except Exception as e:
            last = {'error': str(e)}
        time.sleep(2)
    last['filled'] = False
    last['reason'] = 'timeout'
    return last

def buy_safe(stock_code: str, quantity: int, price: Optional[float] = None,
             verify: bool = True) -> dict:
    """安全买入：rc=0 + status=4 双校验。返回 {ok, order_resp, fill_info}"""
    order_resp = buy(stock_code, quantity, price)
    out = {'ok': True, 'order_resp': order_resp, 'stage': 'submitted'}
    if verify:
        fill = verify_filled(stock_code, quantity, drt=1)
        out['fill_info'] = fill
        out['ok'] = bool(fill.get('filled'))
        out['stage'] = 'filled' if out['ok'] else 'submit_only'
    return out

def sell_safe(stock_code: str, quantity: int, price: Optional[float] = None,
              verify: bool = True) -> dict:
    """安全卖出：rc=0 + status=4 双校验"""
    order_resp = sell(stock_code, quantity, price)
    out = {'ok': True, 'order_resp': order_resp, 'stage': 'submitted'}
    if verify:
        fill = verify_filled(stock_code, quantity, drt=2)
        out['fill_info'] = fill
        out['ok'] = bool(fill.get('filled'))
        out['stage'] = 'filled' if out['ok'] else 'submit_only'
    return out

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
