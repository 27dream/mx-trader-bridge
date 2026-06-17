"""v3 中央参数表 — 全自动版 + 5道护栏

【调参规则】
- TUNABLE 白名单内的参数：reflector 可自动调（受 BOUNDS 限制）
- 其他参数：硬编码不可改（含日亏熔断、集合竞价禁单等核心风控）
- 每次自动调参写入 params_history 表，可一键 revert
"""

# ============== 策略开关（True/False） ==============
STRATEGIES_ENABLED = {
    'A_pullback':     True,
    'B_first_limit':  True,
    'C_main_inflow':  True,
}

# ============== 全局风控（不可调） ==============
RISK = {
    'max_daily_loss_pct': -0.03,
    'max_position_per_stock_pct': 0.40,
    'max_total_position_pct': 1.00,
    'min_cash_reserve': 0.0,
    'block_periods': [
        ('09:15', '09:25'),
        ('14:57', '15:00'),
    ],
    'force_exit_time': '14:50',
    'min_hold_days_for_time_exit': 3,
}

# ============== A 策略：强势股回调 ==============
STRATEGY_A = {
    'name': '强势股回调',
    'lookback_days': 20,
    'min_gain_pct': 15.0,
    'pullback_to_ma': 10,
    'max_picks': 3,
    'sl_pct': -0.03,
    'tp_pct': 0.06,
    'trail_trigger_pct': 0.05,
    'trail_giveback_pct': 0.02,
    'max_position_pct': 0.20,
    'min_confidence': 0.6,
}

# ============== B 策略：首板跟进 ==============
STRATEGY_B = {
    'name': '首板跟进',
    'market_cap_min': 30,
    'market_cap_max': 150,
    'max_picks': 3,
    'sl_pct': -0.04,
    'tp_pct': 0.07,
    'trail_trigger_pct': 0.05,
    'trail_giveback_pct': 0.025,
    'max_position_pct': 0.20,
    'min_confidence': 0.65,
}

# ============== C 策略：主力净流入 ==============
STRATEGY_C = {
    'name': '主力净流入',
    'main_inflow_min': 5000,
    'volume_ratio_min': 2.0,
    'max_picks': 2,
    'sl_pct': -0.03,
    'tp_pct': 0.05,
    'trail_trigger_pct': 0.04,
    'trail_giveback_pct': 0.02,
    'max_position_pct': 0.20,
    'min_confidence': 0.6,
}

STRATEGY_PARAMS = {
    'A_pullback': STRATEGY_A,
    'B_first_limit': STRATEGY_B,
    'C_main_inflow': STRATEGY_C,
}

# ============== 挂单策略（不可调） ==============
ORDER = {
    'buy_price_step': ['target', 'ask1', 'last_plus_05', 'limit_up'],
    'sell_price_step': ['bid1', 'bid1_minus_tick', 'last', 'limit_down'],
    'max_attempts': 4,
    'wait_seconds_per_attempt': 30,
    'tick_size_main': 0.01,
    'tick_size_chinext': 0.01,
}

# ============== Monitor ==============
MONITOR = {
    'tick_interval_seconds': 60,
    'price_source': 'tencent',
}

# ============== Picker LLM ==============
PICKER_LLM = {
    'use_llm_for_ranking': True,
    'temperature': 0.3,
    'max_per_strategy_to_llm': 15,
}

# ============== 反思器（自动调参开关） ==============
REFLECTOR = {
    'daily_review_enabled': True,
    'weekly_review_enabled': True,
    'auto_apply_param_patch': True,        # ✅ v3：开启自动调参
    'min_samples_to_tune': 10,             # 护栏3：样本量门槛
    'max_single_change_pct': 0.15,         # 护栏4：单次幅度
    'max_3week_cumulative_pct': 0.30,      # 护栏4：3周累计
    'rollback_winrate_ratio': 0.7,         # 护栏5：胜率<0.7×历史均值回滚
    'rollback_consec_loss_days': 3,        # 护栏5：连亏3天回滚
    'rollback_loss_threshold': -0.02,      # 护栏5：每日<-2%才计为亏损
}


# ============== ★ 护栏 1：可调参数白名单 ==============
TUNABLE = {
    'STRATEGY_A': ['sl_pct', 'tp_pct', 'trail_trigger_pct', 'trail_giveback_pct',
                   'max_picks', 'max_position_pct', 'min_gain_pct'],
    'STRATEGY_B': ['sl_pct', 'tp_pct', 'trail_trigger_pct', 'trail_giveback_pct',
                   'max_picks', 'max_position_pct'],
    'STRATEGY_C': ['sl_pct', 'tp_pct', 'trail_trigger_pct', 'trail_giveback_pct',
                   'max_picks', 'max_position_pct', 'main_inflow_min'],
    'STRATEGIES_ENABLED': ['A_pullback', 'B_first_limit', 'C_main_inflow'],
}

# ============== ★ 护栏 2：参数取值边界 (min, max) ==============
BOUNDS = {
    'sl_pct':              (-0.06, -0.015),  # 止损范围 -6% ~ -1.5%
    'tp_pct':              (0.03,   0.12),   # 止盈 +3% ~ +12%
    'trail_trigger_pct':   (0.02,   0.10),
    'trail_giveback_pct':  (0.01,   0.05),
    'max_picks':           (1,      8),
    'max_position_pct':    (0.10,   0.30),   # 单票仓位 10%~30%
    'min_gain_pct':        (5.0,    30.0),
    'main_inflow_min':     (1000,   20000),  # 万元
}


# ============== 全局交易/卖出默认（monitor + executor 用） ==============
# 注：reflector 调参以策略级 STRATEGY_A/B/C 为主，这里是"未指定策略"时的兜底
TRADING = {
    'max_position_pct': 0.20,        # 单票上限（与 RISK.max_position_per_stock_pct 互不冲突）
    'min_cash_per_trade': 1000,      # 单笔最低金额
}

# sell 参数（策略级 sl/tp 仅在 picker 输出候选时参考；实际卖出用全局 SELL）
SELL = {
    'hard_stop_pct': -0.03,          # 硬止损 -3%
    'take_profit_pct': 0.50,         # 已废弃（固定止盈已移除），由移动止损替代
    'trail_peak_trigger': 0.05,      # 移动止损：涨超 5% 后激活
    'trail_drawdown':      0.02,
    'force_sell_time':    '14:50',
    'max_hold_days':       3,
}


def snapshot():
    return {
        'strategies_enabled': STRATEGIES_ENABLED,
        'risk': RISK,
        'strategy_params': STRATEGY_PARAMS,
        'order': ORDER,
        'monitor': MONITOR,
        'picker_llm': PICKER_LLM,
        'reflector': REFLECTOR,
    }


def get_strategy(strategy_id: str) -> dict:
    """统一访问入口：所有模块通过这里取参数（picker/executor/monitor）"""
    return STRATEGY_PARAMS.get(strategy_id, {})


if __name__ == '__main__':
    import json
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
