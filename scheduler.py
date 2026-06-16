"""scheduler v2 - 状态机主循环
每分钟 tick 一次：根据时间窗口 + 系统状态 决定调用哪个模块。
取代原 cron 的"按点触发"，改为"按状态推进"。

状态机：
  IDLE → PICKING → BUYING → MONITORING → CLOSING → REPORTING → IDLE

时间窗口（A股交易日，节假日由调用方过滤）：
  08:30-09:15  PICKING（盘前选股）
  09:25-09:30  PRE_OPEN（集合竞价后等开盘）
  09:30-11:30  BUYING + MONITORING（早盘）
  11:30-13:00  LUNCH（休市）
  13:00-14:50  MONITORING（午后盯盘）
  14:50-15:00  CLOSING（收盘前强平 + 撤所有未成挂单）
  15:00-15:30  REPORTING（日报）
  15:30+       IDLE
"""
import os, sys, time, json, traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import db, notifier

TICK_SEC = int(os.getenv('SCHED_TICK_SEC', '60'))  # 默认 60s 一轮
MAX_TICKS = int(os.getenv('SCHED_MAX_TICKS', '0'))  # 0=无限
DRY_RUN = os.getenv('SCHED_DRY_RUN', '0') == '1'    # 1=只走流程不下单
FORCE_PHASE = os.getenv('SCHED_FORCE_PHASE', '')    # 强制阶段，如 PICKING/TRADING_AM/CLOSING/REPORTING
FORCE_TRADING_DAY = os.getenv('SCHED_FORCE_TRADING_DAY', '0') == '1'  # 周末测试用

STATE_FILE = os.path.join(os.path.dirname(__file__), '.sched_state.json')

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {'state': 'IDLE', 'today': '', 'last_tick': '', 'picked': False, 'reported': False}

def _save_state(s: dict):
    s['last_tick'] = datetime.now().isoformat(timespec='seconds')
    json.dump(s, open(STATE_FILE, 'w'), ensure_ascii=False, indent=2)

def _phase(now_hm: str) -> str:
    if '08:30' <= now_hm < '09:15': return 'PICKING'
    if '09:15' <= now_hm < '09:30': return 'PRE_OPEN'
    if '09:30' <= now_hm < '11:30': return 'TRADING_AM'
    if '11:30' <= now_hm < '13:00': return 'LUNCH'
    if '13:00' <= now_hm < '14:50': return 'TRADING_PM'
    if '14:50' <= now_hm < '15:00': return 'CLOSING'
    if '15:00' <= now_hm < '15:30': return 'REPORTING'
    return 'IDLE'

def _is_trading_day() -> bool:
    """周一到周五 = 交易日（节假日需调用方提前过滤；这里做基础保护）"""
    return datetime.now().weekday() < 5

# ---------- 各阶段动作 ----------
def _do_pick(today: str):
    print(f"[{today}] 📋 PICKING - 调用 picker.py")
    try:
        import importlib, picker
        importlib.reload(picker)
        candidates = picker.main()
        if isinstance(candidates, dict):
            candidates = candidates.get('picks', [])
        print(f"  ✓ 选出 {len(candidates)} 只候选")
        # 推送选股结果（前 5 只）
        if candidates:
            try:
                notifier.notify_decision('日内策略', candidates[:5])
            except Exception:
                notifier.notify(f"📋 今日候选 {len(candidates)} 只", level='info')
        else:
            notifier.notify(f"📋 [{today}] PICKING - 0 候选（可能非交易时段或 API 限流）", level='info')
        return True
    except Exception as e:
        traceback.print_exc()
        notifier.alert(f"picker 异常: {e}", level='error', title='选股失败')
        return False

def _do_buy(today: str):
    if DRY_RUN:
        print(f"[{today}] 💤 DRY_RUN - 跳过 executor.execute_buys()")
        return
    try:
        import importlib, executor
        importlib.reload(executor)
        executor.execute_buys()
    except Exception as e:
        traceback.print_exc()
        notifier.alert(f"executor 异常: {e}", level='error', title='买入失败')

def _do_monitor():
    if DRY_RUN:
        print(f"  💤 DRY_RUN - 跳过 monitor.check_and_exit()")
        return
    try:
        import importlib, monitor
        importlib.reload(monitor)
        monitor.check_and_exit()
    except Exception as e:
        traceback.print_exc()
        notifier.alert(f"monitor 异常: {e}", level='error', title='盯盘失败')

def _do_closing():
    """收盘前：撤所有未成挂单 + 强平触发已在 monitor 内"""
    print(f"🔚 CLOSING - 撤未成挂单")
    try:
        import trader
        # 撤所有委托（trader 若无该方法，跳过）
        if hasattr(trader, 'cancel_all_open_orders'):
            trader.cancel_all_open_orders()
        _do_monitor()  # 触发 force_exit
    except Exception as e:
        traceback.print_exc()
        notifier.alert(f"closing 异常: {e}", level='error', title='收盘异常')

def _do_report(today: str):
    """REPORTING：每日 recap.run_recap() + 周五额外 reflect.weekly_reflect()"""
    print(f"[{today}] 📊 REPORTING - 生成日报")
    try:
        import importlib, recap
        importlib.reload(recap)
        recap.run_recap()
        notifier.notify(f"📊 [{today}] 日报已生成（详情见 logs/recap_{today.replace('-','')}.log）", level='success')
        # 周五额外跑周复盘
        if datetime.now().weekday() == 4:
            print(f"[{today}] 🪞 周五 - 触发 weekly_reflect")
            import reflect
            importlib.reload(reflect)
            reflect.weekly_reflect()
            notifier.notify(f"🪞 [{today}] 周复盘完成 + 自动调参检查", level='success')
    except Exception as e:
        traceback.print_exc()
        notifier.alert(f"recap/reflect 异常: {e}", level='error', title='日报失败')

# ---------- 主循环 ----------
def tick():
    s = _load_state()
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    now_hm = now.strftime('%H:%M')

    # 跨日重置
    if s.get('today') != today:
        s = {'state': 'IDLE', 'today': today, 'picked': False, 'reported': False}

    if not _is_trading_day() and not FORCE_TRADING_DAY:
        s['state'] = 'WEEKEND'
        _save_state(s)
        print(f"[{now_hm}] 周末/非交易日，跳过")
        return

    phase = FORCE_PHASE or _phase(now_hm)
    if FORCE_PHASE:
        print(f"  ⚠️ FORCE_PHASE={FORCE_PHASE}")
    print(f"[{now_hm}] phase={phase} state={s['state']} picked={s['picked']} reported={s['reported']}")

    if phase == 'PICKING' and not s['picked']:
        if _do_pick(today):
            s['picked'] = True
            s['state'] = 'PICKING_DONE'
    elif phase == 'TRADING_AM':
        s['state'] = 'TRADING'
        _do_buy(today)
        _do_monitor()
    elif phase == 'TRADING_PM':
        s['state'] = 'TRADING'
        _do_monitor()
    elif phase == 'CLOSING':
        s['state'] = 'CLOSING'
        _do_closing()
    elif phase == 'REPORTING' and not s['reported']:
        if _do_report(today) is not False:
            s['reported'] = True
            s['state'] = 'DONE'
    elif phase in ('LUNCH', 'PRE_OPEN', 'IDLE'):
        s['state'] = phase

    _save_state(s)

def main_loop():
    print(f"🚀 scheduler v2 启动 tick={TICK_SEC}s max_ticks={MAX_TICKS or '∞'}")
    n = 0
    while True:
        try:
            tick()
        except Exception as e:
            traceback.print_exc()
            notifier.alert(f"scheduler tick 崩溃: {e}", level='error', title='调度器异常')
        n += 1
        if MAX_TICKS and n >= MAX_TICKS:
            print(f"达到最大 tick 数 {MAX_TICKS}，退出")
            break
        time.sleep(TICK_SEC)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'once':
        tick()
    else:
        main_loop()
