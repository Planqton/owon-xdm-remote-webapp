#####################################################################################
# main.py - bootloader / trust root (update via USB only!)
#
# Safety concept (A/B style, like a phone):
#   - Trust root = main.py + wd.py  (tiny, stable; flash via USB)
#   - App set    = dbg.py, ota.py, recovery.py, wifi_manager.py, app.py
#                  -> saved as a complete known-good snapshot under /good/
#                     once running stably (app._mark_healthy).
#
# Flow:
#   - Arm the watchdog as early as possible (hang -> reset).
#   - Increment the boot counter. app.run() clears it after ~14 s stable + snapshots.
#   - Crash/syntax error in the app -> recovery.run() (WiFi + OTA + console).
#   - Too many failed boots (crash OR hang) -> restore the ENTIRE good/ set
#     and reboot/recover. No USB needed.
#####################################################################################

import wd
wd.start(30000)

ROLLBACK_AT = 4


def _rd(n):
    try:
        with open(n) as f:
            return f.read().strip()
    except Exception:
        return ''


def _wr(n, v):
    try:
        with open(n, 'w') as f:
            f.write(str(v))
    except Exception:
        pass


def _restore_good():
    """Restore the complete app set from /good/ (chunked, low-mem).
    Returns: number of restored files (0 = no snapshot)."""
    import os
    try:
        files = os.listdir('good')
    except Exception:
        return 0
    n = 0
    for fn in files:
        try:
            with open('good/' + fn, 'rb') as s, open(fn, 'wb') as d:
                while True:
                    b = s.read(512)
                    if not b:
                        break
                    d.write(b)
            n += 1
        except Exception:
            pass
    return n


try:
    _cnt = int(_rd('boot.cnt') or '0')
except Exception:
    _cnt = 0
_cnt += 1
_wr('boot.cnt', _cnt)

if _cnt >= ROLLBACK_AT:
    _wr('boot.cnt', '0')
    try:
        _rc = int(_rd('restored.cnt') or '0')
    except Exception:
        _rc = 0
    _n = _restore_good()
    try:
        import dbg
        dbg.log('BOOT', 'boot-loop ({} failed boots) -> good/ restore: {} file(s), attempt {}'.format(_cnt, _n, _rc + 1))
    except Exception:
        pass
    if _n > 0 and _rc < 2:
        # Known-good set restored -> reboot straight back into the app.
        _wr('restored.cnt', _rc + 1)
        import time, machine
        time.sleep(1)
        machine.reset()
    else:
        # No snapshot, or restore did not help repeatedly -> recovery.
        import recovery
        recovery.run('boot-loop -> good/ restore x{} ({} files)'.format(_rc, _n))
else:
    try:
        import app
        app.run()
    except Exception as e:
        try:
            import sys, io, dbg
            buf = io.StringIO()
            sys.print_exception(e, buf)
            for ln in buf.getvalue().split('\n'):
                if ln.strip():
                    dbg.log('CRASH', ln)
        except Exception:
            try:
                import dbg
                dbg.log('CRASH', repr(e))
            except Exception:
                print('CRASH:', e)
        try:
            import recovery
            recovery.run(e)
        except Exception as e2:
            # App AND recovery broken -> reset; the boot counter leads to rollback.
            try:
                import dbg
                dbg.log('CRASH', 'recovery failed: {}'.format(e2))
            except Exception:
                pass
            import time, machine
            time.sleep(2)
            machine.reset()
