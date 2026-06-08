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


def _append_crash(text):
    """Append a crash/boot note to the persistent crash log (capped ~3 KB)."""
    try:
        old = ''
        try:
            with open('crash.log') as f:
                old = f.read()
        except Exception:
            old = ''
        with open('crash.log', 'w') as f:
            f.write((old + text + '\n')[-3000:])
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
    _prev = int(_rd('boot.cnt') or '0')
except Exception:
    _prev = 0
_cnt = _prev + 1
_wr('boot.cnt', _cnt)
# A previous boot that never reached "healthy" (crash, hang/WDT, or power loss
# in the first seconds) -> mark unclean so the UI can show a notice.
if _prev > 0:
    _wr('unclean.flag', str(_prev))
    _append_crash('[boot] unclean restart (previous boot.cnt={})'.format(_prev))

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
        tb = repr(e)
        try:
            import sys, io
            buf = io.StringIO()
            sys.print_exception(e, buf)
            tb = buf.getvalue()
        except Exception:
            pass
        _append_crash('[crash] ' + tb)
        _wr('unclean.flag', 'crash')
        try:
            import dbg
            for ln in tb.split('\n'):
                if ln.strip():
                    dbg.log('CRASH', ln)
        except Exception:
            pass
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
