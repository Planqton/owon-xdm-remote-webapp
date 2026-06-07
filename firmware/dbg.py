#####################################################################################
# dbg.py - shared logging with an in-RAM ring buffer for the web console.
#
# Use dbg.log(tag, msg) everywhere instead of print(). Lines are printed to the
# serial console AND kept in a capped ring buffer that /console (see ota.py) shows.
#####################################################################################

import time

_BUF = []
_MAX = 250


def log(tag, msg):
    try:
        t = time.localtime()
        ms = time.ticks_ms() % 1000
        ts = "{:02d}:{:02d}:{:02d}.{:03d}".format(t[3], t[4], t[5], ms)
        line = "[{}][{:>6}] {}".format(ts, tag, msg)
    except Exception:
        line = "[??][{}] {}".format(tag, msg)
    print(line)
    try:
        _BUF.append(line)
        n = len(_BUF)
        if n > _MAX:
            del _BUF[0:n - _MAX]
    except Exception:
        pass


def text():
    # slice -> atomic copy, safe to join while poller thread appends
    return "\n".join(_BUF[:])


def clear():
    try:
        _BUF[:] = []
    except Exception:
        pass
