"""Run every module self-check. One command, offline, no key needed.

    python selfcheck.py
"""
from monitor.tracer import _selfcheck as tracer_check
from monitor.sdk import _selfcheck as sdk_check
from monitor.cost import _selfcheck as cost_check
from monitor.eval import _selfcheck as eval_check
from monitor.drift import _selfcheck as drift_check
from monitor.alerts import _selfcheck as alerts_check

if __name__ == "__main__":
    tracer_check()
    sdk_check()
    cost_check()
    eval_check()
    drift_check()
    alerts_check()
    print("all self-checks OK")
