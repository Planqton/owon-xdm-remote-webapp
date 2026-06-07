#####################################################################################
# recovery.py - emergency OTA server when app.py fails to import/run.
#
# main.py calls recovery.run(err) if `import app; app.run()` raises. This connects
# WiFi using saved credentials (captive portal if none) and serves the OTA upload
# page so a fixed app.py can be pushed over WiFi — no USB required.
#####################################################################################

import network
import socket
import time
import gc
from wifi_manager import WifiManager
import ota
import dbg
try:
    import wd
except Exception:
    class wd:
        @staticmethod
        def feed():
            pass


def run(err=None):
    dbg.log('RECOV', 'app.py failed: {} -> starting recovery server'.format(err))
    try:
        WifiManager(ssid='OWON-XDM-Remote-Setup', password='', reboot=False, debug=True).connect()
    except Exception as e:
        dbg.log('RECOV', 'wifi error: {}'.format(e))
    try:
        ip = network.WLAN(network.STA_IF).ifconfig()[0]
    except Exception:
        ip = '?'
    dbg.log('RECOV', 'up at http://{}/  reason={}'.format(ip, err))

    sub = "RECOVERY-Modus &mdash; app.py-Fehler: {} &mdash; reparierte Datei hochladen, dann Neustart.".format(err)

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(2)
    s.settimeout(1.0)
    while True:
        wd.feed()
        try:
            cl, addr = s.accept()
        except OSError:
            gc.collect()
            continue
        try:
            cl.settimeout(8.0)
            method, path, query_s, body = ota.read_request(cl)
            if not ota.handle(cl, method, path, query_s, body):
                ota.page(cl, sub=sub)
        except Exception as e:
            dbg.log('RECOV', 'req err: {}'.format(e))
        finally:
            try:
                cl.close()
            except Exception:
                pass
