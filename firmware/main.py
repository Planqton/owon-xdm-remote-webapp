#####################################################################################
# main.py - Bootloader / Trust-Root (nur per USB aktualisieren!)
#
# Sicherheitskonzept (wie A/B beim Handy):
#   - Trust-Root  = main.py + wd.py  (winzig, stabil; via USB flashen)
#   - App-Satz    = dbg.py, ota.py, recovery.py, wifi_manager.py, app.py
#                   -> wird bei stabilem Lauf als kompletter bekannt-guter
#                      Snapshot unter /good/ gesichert (app._mark_healthy).
#
# Ablauf:
#   - Watchdog so früh wie möglich scharf (Hang -> Reset).
#   - Boot-Zähler hoch. app.run() nullt ihn nach ~14 s stabilem Lauf + macht Snapshot.
#   - Crash/Syntaxfehler in App -> recovery.run() (WLAN + OTA + Konsole).
#   - Bei zu vielen Fehlboots (Crash ODER Hang) -> KOMPLETTEN good/-Satz
#     wiederherstellen und Recovery starten. Kein USB nötig.
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
    """Stellt den kompletten App-Satz aus /good/ wieder her (chunked, low-mem).
    Rückgabe: Anzahl wiederhergestellter Dateien (0 = kein Snapshot)."""
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
        dbg.log('BOOT', 'boot-loop ({} Fehlboots) -> good/ restore: {} Datei(en), Versuch {}'.format(_cnt, _n, _rc + 1))
    except Exception:
        pass
    if _n > 0 and _rc < 2:
        # Bekannt-guten Stand wiederhergestellt -> automatisch in die App neu starten.
        _wr('restored.cnt', _rc + 1)
        import time, machine
        time.sleep(1)
        machine.reset()
    else:
        # Kein Snapshot, oder Wiederherstellung half mehrfach nicht -> Recovery.
        import recovery
        recovery.run('boot-loop -> good/ restore x{} ({} Dateien)'.format(_rc, _n))
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
            # App UND Recovery kaputt -> Reset; Boot-Zähler führt zum Rollback.
            try:
                import dbg
                dbg.log('CRASH', 'recovery failed: {}'.format(e2))
            except Exception:
                pass
            import time, machine
            time.sleep(2)
            machine.reset()
