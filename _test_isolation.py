"""watchdog 多策略 tp_pct 隔离测试 — 验证 _patch_params_file 不会跨段匹配错"""
import sys, os, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from param_engine import _patch_params_file
import params as P

# 起始
importlib.reload(P)
print(f'起始: A={P.STRATEGY_A["tp_pct"]} B={P.STRATEGY_B["tp_pct"]} C={P.STRATEGY_C["tp_pct"]}')

# 改 A
_patch_params_file('STRATEGY_A', 'tp_pct', 0.08)
importlib.reload(P)
print(f'改A: A={P.STRATEGY_A["tp_pct"]} B={P.STRATEGY_B["tp_pct"]} C={P.STRATEGY_C["tp_pct"]} (B/C 不变?)')
assert P.STRATEGY_A['tp_pct'] == 0.08
assert P.STRATEGY_B['tp_pct'] == 0.07
assert P.STRATEGY_C['tp_pct'] == 0.05

# 改 C
_patch_params_file('STRATEGY_C', 'tp_pct', 0.04)
importlib.reload(P)
print(f'改C: A={P.STRATEGY_A["tp_pct"]} B={P.STRATEGY_B["tp_pct"]} C={P.STRATEGY_C["tp_pct"]}')
assert P.STRATEGY_A['tp_pct'] == 0.08
assert P.STRATEGY_C['tp_pct'] == 0.04

# 还原
_patch_params_file('STRATEGY_A', 'tp_pct', 0.06)
_patch_params_file('STRATEGY_C', 'tp_pct', 0.05)
importlib.reload(P)
print(f'还原: A={P.STRATEGY_A["tp_pct"]} B={P.STRATEGY_B["tp_pct"]} C={P.STRATEGY_C["tp_pct"]}')
print('✅ 多段隔离正常')
