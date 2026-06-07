#####################################################################################
# wd.py - shared hardware watchdog. Armed very early in main.py and fed from EVERY
# long-running loop (app poller/web, recovery server, wifi portal/connect, startup).
#
# Guarantee: if the firmware hangs ANYWHERE for longer than the timeout, the WDT
# resets the chip -> main.py's boot counter climbs -> auto-rollback to app_good.py.
# Create a file named 'nowdt' to disable it for USB/REPL debugging.
#####################################################################################

import machine
import os

_wdt = None


def start(timeout_ms=30000):
    global _wdt
    if _wdt is not None:
        return
    try:
        if 'nowdt' in os.listdir():
            return
    except Exception:
        pass
    try:
        _wdt = machine.WDT(timeout=timeout_ms)
    except Exception:
        _wdt = None


def feed():
    if _wdt is not None:
        try:
            _wdt.feed()
        except Exception:
            pass


def active():
    return _wdt is not None
