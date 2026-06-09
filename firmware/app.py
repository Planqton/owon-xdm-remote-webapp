#####################################################################################
"""
app.py - OWON XDM1041 professional web bench (ESP32-WROOM, MicroPython)

Full-window instrument UI (tabs V / A / Ohm / Cap / Freq / Temp with sub-modes),
sampling-rate (Low/Mid/High), range (auto + manual), hold, trigger (auto/single),
min/max/avg + sparkline. Clean JSON API under /api for USB-free control/debug.

Background poller owns the UART: applies pending function/rate/range changes, then
polls MEAS? continuously and caches the latest reading. Web threads only read caches
(or enqueue requests), so the display stays fast (~3-5/s, meter-limited).

API:
  GET  /api/reading              -> {ok,value,raw,age}
  GET  /api/status               -> full state (function,rate,range,auto,idn,...)
  GET  /api/function?set=KEY     -> switch measurement function
  GET  /api/rate?set=S|M|F       -> sampling rate
  GET  /api/range?set=auto|up|down
  GET  /api/scpi?cmd=...         -> run one raw SCPI command, return response
  + shared /ota /upload /reboot /console /log /logclear (ota.py)
"""
#####################################################################################

from machine import UART, Pin, freq
import network
import wd
import time
import ubinascii
import gc
import os
import socket
try:
    import _thread
except Exception:
    _thread = None
from wifi_manager import WifiManager
import ota
from dbg import log

_BOOT_TICKS = time.ticks_ms()   # for the console /uptime command

# ─── UART / Pins ────────────────────────────────────────────────────────────────--
UART_NUM, BAUDRATE, TX_PIN, RX_PIN, LED_PIN = 2, 115200, 17, 16, 2

# ─── Timing ─────────────────────────────────────────────────────────────────────--
IDLE_TIMEOUT_MS = 2000
IDN_MAX_ATTEMPTS = 10
SET_MAX_ATTEMPTS = 10
MEAS_TIMEOUT_MS = 500
SWITCH_SKIP = 2

# ─── SCPI ─────────────────────────────────────────────────────────────────────────
IDN_COMMAND  = b"*IDN?\r\n"
MEAS_COMMAND = b"MEAS?\r\n"
EXPECTED_IDN_KEYWORDS = (b"OWON", b"XDM")

# function key -> (CONF command, unit-group)   group: V A R D C Hz T
MODE_MAP = {
    'VDC':  ('CONF:VOLT:DC', 'V'),
    'VAC':  ('CONF:VOLT:AC', 'V'),
    'ADC':  ('CONF:CURR:DC', 'A'),
    'AAC':  ('CONF:CURR:AC', 'A'),
    'RES':  ('CONF:RES',     'R'),
    'CONT': ('CONF:CONT',    'R'),
    'DIOD': ('CONF:DIOD',    'D'),
    'CAP':  ('CONF:CAP',     'C'),
    'FREQ': ('CONF:FREQ',    'Hz'),
    'TEMP': ('CONF:TEMP',    'T'),
}
# tabs: (tab label, [(key, sub-label), ...])
TABS = [
    ('V',   [('VDC', 'DC'), ('VAC', 'AC')]),
    ('A',   [('ADC', 'DC'), ('AAC', 'AC')]),
    ('Ω', [('RES', 'Resistance'), ('CONT', 'Continuity'), ('DIOD', 'Diode')]),
    ('Cap', [('CAP', 'Capacitance')]),
    ('Hz',  [('FREQ', 'Frequency')]),
    ('°C', [('TEMP', 'Temperature')]),
]
DEFAULT_MODE = 'VDC'

CODE_TIMESTAMP = "2026-06-06 (app v15: topbar toggles + RAM meter (gc))"

# ─── Globals ──────────────────────────────────────────────────────────────────────
uart_comm = None
tx_pin = rx_pin = led = None
_idn = ''
# requested vs applied
_func_req = DEFAULT_MODE
_func = None
_rate_req = 'F'
_rate = None
_range_req = None        # None | 'auto' | 'up' | 'down'
_range_idx = 3
_range_lbl = ''
_auto = 1
_mem_max = 0              # high-water of (free+alloc); stable heap ceiling for the RAM meter
_follow_dev = False       # follow device-side function changes via FUNC? readback (toggle: /api/devsync)
_follow_cnt = 0
FOLLOW_EVERY = 6          # read FUNC? every Nth poll cycle while following (keeps MEAS? rate up)
FUNC_READ_MAP = {'VOLT': 'VDC', 'VOLT AC': 'VAC', 'CURR': 'ADC', 'CURR AC': 'AAC',
                 'RES': 'RES', 'CONT': 'CONT', 'DIOD': 'DIOD', 'CAP': 'CAP', 'FREQ': 'FREQ'}
_skip = 0
# cached reading
_v_ok, _v_val, _v_raw, _v_ts = False, None, '', 0
_meas_t = []   # ms timestamps of recent successful reads (for live samples/s)
PAGE_BYTES = None   # (unused; page is streamed in chunks)
_healthy = False    # set once the app has run stably (saves app_good.py, clears boot.cnt)
# debug RPC
_rpc_cmd, _rpc_resp, _rpc_seq = None, '', 0
_rpc_lock = _thread.allocate_lock() if _thread else None


def blink(times, on_t, off_t):
    for _ in range(times):
        led.value(0); time.sleep(on_t)
        led.value(1); time.sleep(off_t)


# ─── UART ───────────────────────────────────────────────────────────────────────--

def init_uart():
    global uart_comm
    if uart_comm is None:
        uart_comm = UART(UART_NUM, BAUDRATE, tx=TX_PIN, rx=RX_PIN,
                         bits=8, parity=None, stop=1, timeout=300)
        time.sleep_ms(10)
    return uart_comm


def reopen_uart():
    global uart_comm
    if uart_comm is not None:
        try:
            uart_comm.deinit()
        except Exception:
            pass
        uart_comm = None
        time.sleep_ms(10)
    return init_uart()


def query(cmd, timeout_ms=MEAS_TIMEOUT_MS):
    if isinstance(cmd, str):
        cmd = cmd.encode()
    if not cmd.endswith(b"\r\n"):
        cmd += b"\r\n"
    u = init_uart()
    while u.any():
        u.read()
    u.write(cmd)
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    resp = b""
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if u.any():
            resp += u.read() or b""
            if b"\n" in resp:
                break
        else:
            time.sleep_ms(2)
    return resp.decode().strip() if resp else ""


def send_cmd(cmd):
    if isinstance(cmd, str):
        cmd = cmd.encode()
    if not cmd.endswith(b"\r\n"):
        cmd += b"\r\n"
    init_uart().write(cmd)


# ─── Startup handshake ────────────────────────────────────────────────────────────

def wait_idle():
    log('STAGE', '1 - Idle-line check')
    last = rx_pin.value()
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < IDLE_TIMEOUT_MS:
        time.sleep_ms(10)
        if rx_pin.value() != last:
            log('STAGE', 'Abort: UART activity')
            return False
    log('STAGE', 'Idle OK')
    return True


def wait_ready():
    global _idn
    log('STAGE', '2 - *IDN?')
    u = init_uart()
    for _ in range(IDN_MAX_ATTEMPTS):
        wd.feed()
        u.write(IDN_COMMAND)
        time.sleep(1.0)
        resp = u.readline(); out = resp.decode().strip() if resp else ''
        log('UART', 'IDN: {}'.format(out))
        if any(k.decode() in out for k in EXPECTED_IDN_KEYWORDS):
            _idn = out
            return True
        time.sleep(1.0)
    return False


def set_rate_high():
    log('STAGE', '3 - RATE F')
    u = init_uart()
    for _ in range(SET_MAX_ATTEMPTS):
        wd.feed()
        u.write(b"RATE F\r\n"); time.sleep(0.2)
        u.write(b"RATE?\r\n"); time.sleep(0.2)
        resp = u.readline(); out = resp.decode().strip() if resp else ''
        if out == 'F':
            return True
        time.sleep(1.0)
    return False


def run_sequence():
    global _rate
    log('RUN', 'Startup sequence')
    try:
        tx_pin.init(Pin.IN)
    except Exception:
        pass
    if not wait_idle():
        blink(5, 0.1, 0.1)
        return False
    reopen_uart()
    ready = wait_ready()
    rate_ok = set_rate_high() if ready else False
    if rate_ok:
        _rate = 'F'
    blink(2, 0.4, 0.4) if (ready and rate_ok) else blink(5, 0.1, 0.1)
    log('RUN', 'ready={} rate_ok={}'.format(ready, rate_ok))
    return ready


# ─── Poller (UART owner) ──────────────────────────────────────────────────────────

def _refresh_status():
    global _range_lbl, _auto, _rate
    try:
        _range_lbl = query('RANGE?')
    except Exception:
        pass
    try:
        a = query('AUTO?')
        _auto = 1 if a.strip() == '1' else 0
    except Exception:
        pass
    try:
        r = query('RATE?')
        if r in ('S', 'M', 'F'):
            _rate = r
    except Exception:
        pass


def poll_once():
    global _v_ok, _v_val, _v_raw, _v_ts, _func, _rate, _range_req, _range_idx, _skip
    global _rpc_cmd, _rpc_resp, _rpc_seq, _func_req, _follow_cnt

    # 1) RPC (from /api/scpi and the SCPI-TCP server): a query waits for a reply,
    #    a write (no '?') is only sent -> no 1s timeout.
    if _rpc_cmd is not None:
        c = _rpc_cmd
        try:
            if c.strip().endswith('?'):
                _rpc_resp = query(c, 1000)
            else:
                send_cmd(c)
                time.sleep_ms(50)
                _rpc_resp = ''
        except Exception as e:
            _rpc_resp = 'ERR {}'.format(e)
        _rpc_cmd = None
        _rpc_seq += 1
        return

    # 2) function switch
    if _func_req != _func:
        conf, g = MODE_MAP.get(_func_req, ('CONF:VOLT:DC', 'V'))
        log('MODE', '{} ({})'.format(_func_req, conf))
        try:
            send_cmd(conf); time.sleep_ms(200)
            send_cmd('AUTO 1'); time.sleep_ms(120)   # autorange by default
        except Exception as e:
            log('MODE', 'err {}'.format(e))
        _func = _func_req
        _skip = SWITCH_SKIP
        _v_ok = False
        _refresh_status()
        return

    # 3) sampling rate
    if _rate_req != _rate:
        try:
            send_cmd('RATE ' + _rate_req); time.sleep_ms(150)
        except Exception as e:
            log('RATE', 'err {}'.format(e))
        _rate = _rate_req
        _refresh_status()
        return

    # 4) range change
    if _range_req is not None:
        rr = _range_req; _range_req = None
        try:
            if rr == 'auto':
                send_cmd('AUTO 1')
            else:
                if rr == 'up':
                    _range_idx = min(_range_idx + 1, 6)
                elif rr == 'down':
                    _range_idx = max(_range_idx - 1, 1)
                send_cmd('RANGE ' + str(_range_idx))
            time.sleep_ms(150)
        except Exception as e:
            log('RANGE', 'err {}'.format(e))
        _skip = SWITCH_SKIP
        _v_ok = False
        _refresh_status()
        return

    # 4.5) follow a function change made ON THE DEVICE (throttled FUNC? readback)
    if _follow_dev:
        _follow_cnt += 1
        if _follow_cnt >= FOLLOW_EVERY:
            _follow_cnt = 0
            try:
                fr = query('FUNC?')
                if fr:
                    k = FUNC_READ_MAP.get(fr.strip().strip('"').strip().upper())
                    if k and k != _func:
                        log('FOLLOW', 'device -> {}'.format(k))
                        _func = _func_req = k
                        _skip = SWITCH_SKIP
                        _v_ok = False
                        _refresh_status()
            except Exception:
                pass
            return

    # 5) measurement
    raw = query(MEAS_COMMAND)
    if _skip > 0:
        _skip -= 1
        return
    if raw:
        try:
            v = float(raw)
            now = time.ticks_ms()
            _v_val, _v_raw, _v_ts, _v_ok = v, raw, now, True
            _meas_t.append(now)
            if len(_meas_t) > 24:
                _meas_t.pop(0)
            return
        except Exception:
            pass
    _v_ok = False


SNAP_FILES = ('dbg.py', 'ota.py', 'recovery.py', 'wifi_manager.py', 'app.py', 'page.html')


def _mark_healthy():
    """Stable run confirmed: clear the boot counter and save a complete
    known-good snapshot of the app set to /good/, so the launcher can
    restore EVERYTHING after a bad OTA update."""
    global _healthy
    _healthy = True
    try:
        with open('boot.cnt', 'w') as f:
            f.write('0')
    except Exception:
        pass
    try:
        with open('restored.cnt', 'w') as f:
            f.write('0')
    except Exception:
        pass
    try:
        try:
            os.mkdir('good')
        except Exception:
            pass
        cnt = 0
        for fn in SNAP_FILES:
            try:
                with open(fn, 'rb') as s, open('good/' + fn, 'wb') as d:
                    while True:
                        b = s.read(512)
                        if not b:
                            break
                        d.write(b)
                cnt += 1
            except Exception as e:
                log('BOOT', 'snap {}: {}'.format(fn, e))
        log('BOOT', 'healthy: snapshot good/ ({} files)'.format(cnt))
    except Exception as e:
        log('BOOT', 'healthy err {}'.format(e))


def poller():
    log('POLL', 'started')
    n = 0
    while True:
        try:
            poll_once()
        except Exception as e:
            log('POLL', 'err {}'.format(e))
            time.sleep_ms(200)
        n += 1
        if n == 40 and not _healthy:
            _mark_healthy()
        wd.feed()
        time.sleep_ms(10)


# ─── Page (built once) ────────────────────────────────────────────────────────────

def _cfg_js():
    parts = []
    for (tab, subs) in TABS:
        sb = []
        for (k, sl) in subs:
            g = MODE_MAP[k][1]
            sb.append('{{k:"{}",l:"{}",g:"{}"}}'.format(k, sl, g))
        parts.append('{{n:"{}",s:[{}]}}'.format(tab, ",".join(sb)))
    return "[" + ",".join(parts) + "]"


# PAGE is streamed from page.html on flash (keeps ~30KB out of RAM -> heap for WiFi)



def _fmt(v):
    if v is None:
        return "null"
    try:
        return "{:g}".format(v)
    except Exception:
        return "null"


# ─── HTTP handlers ────────────────────────────────────────────────────────────────

def read_net():
    cfg = {'host': 'owon', 'dhcp': 1, 'ip': '', 'mask': '255.255.255.0',
           'gw': '192.168.0.1', 'dns': '192.168.0.1'}
    try:
        with open('net.dat') as f:
            for line in f:
                line = line.strip()
                if '=' in line:
                    k, v = line.split('=', 1)
                    cfg[k] = v
    except Exception:
        pass
    cfg['dhcp'] = 0 if str(cfg.get('dhcp')) in ('0', 'False', 'false') else 1
    cfg['host'] = 'owon'   # hostname is fixed: the device is always reachable at owon.local
    return cfg


def write_net(cfg):
    with open('net.dat', 'w') as f:
        for k in ('host', 'dhcp', 'ip', 'mask', 'gw', 'dns'):
            f.write('{}={}\n'.format(k, cfg.get(k, '')))


def read_follow():
    try:
        with open('dsync.dat') as f:
            return f.read().strip() == '1'
    except Exception:
        return False


def write_follow(v):
    try:
        with open('dsync.dat', 'w') as f:
            f.write('1' if v else '0')
    except Exception:
        pass


def serve_devsync(cl, q):
    global _follow_dev
    s = ota.qparam(q, 'set')
    if s in ('0', '1'):
        _follow_dev = (s == '1')
        write_follow(_follow_dev)
    ota.send(cl, '{{"follow":{}}}'.format(1 if _follow_dev else 0), "application/json")


def serve_net_get(cl):
    net = read_net()
    try:
        cur = network.WLAN(network.STA_IF).ifconfig()[0]
    except Exception:
        cur = ''
    body = ('{{"host":"{}","dhcp":{},"ip":"{}","mask":"{}","gw":"{}","dns":"{}","cur_ip":"{}"}}'
            ).format(net['host'], 1 if net['dhcp'] else 0, net['ip'], net['mask'],
                     net['gw'], net['dns'], cur)
    ota.send(cl, body, "application/json")


def serve_net_set(cl, q):
    cfg = read_net()
    # 'host' is intentionally NOT settable — owon.local stays fixed (read_net forces it).
    for k in ('ip', 'mask', 'gw', 'dns'):
        v = ota.qparam(q, k)
        if v != '':
            cfg[k] = v.replace('%20', '').strip()
    d = ota.qparam(q, 'dhcp')
    if d in ('0', '1'):
        cfg['dhcp'] = int(d)
    try:
        write_net(cfg)
        ota.send(cl, '{"ok":true}', "application/json")
    except Exception as e:
        ota.send(cl, '{{"ok":false,"err":"{}"}}'.format(e), "application/json", "500 Internal Server Error")


def serve_factory(cl, q):
    """Factory reset: erase WiFi + network config, then reboot into setup AP."""
    if ota.qparam(q, 'confirm') != '1':
        ota.send(cl, '{"ok":false,"err":"need confirm=1"}', "application/json", "400 Bad Request")
        return
    import os
    import machine
    for fn in ('wifi.dat', 'net.dat'):
        try:
            os.remove(fn)
        except Exception:
            pass
    log('BOOT', 'factory reset -> reboot into setup AP')
    ota.send(cl, '{"ok":true}', "application/json")
    try:
        cl.close()
    except Exception:
        pass
    time.sleep_ms(400)
    machine.reset()


def serve_crash(cl, q):
    """Report whether the last session ended uncleanly + the persistent crash log.
    ?clear=1 clears the unclean flag (UI calls this after showing the notice)."""
    import os
    if ota.qparam(q, 'clear') == '1':
        try:
            os.remove('unclean.flag')
        except Exception:
            pass
        ota.send(cl, '{"ok":true}', "application/json")
        return
    unclean = 0
    try:
        os.stat('unclean.flag')
        unclean = 1
    except Exception:
        unclean = 0
    txt = ''
    try:
        with open('crash.log') as f:
            txt = f.read()
    except Exception:
        txt = ''
    txt = (txt.replace('\\', '\\\\').replace('"', '\\"')
              .replace('\r', '').replace('\t', '    ').replace('\n', '\\n'))
    ota.send(cl, '{{"unclean":{},"log":"{}"}}'.format(unclean, txt), "application/json")


# PWA / static assets streamed straight from flash (binary-safe via _send_all).
# path -> (filename, content-type, cache-control)
_STATIC = {
    '/manifest.json': ('manifest.json', 'application/manifest+json', 'no-cache'),
    '/sw.js':         ('sw.js',         'application/javascript',    'no-cache'),
    '/icon-192.png':  ('icon-192.png',  'image/png',                 'max-age=604800'),
    '/icon-512.png':  ('icon-512.png',  'image/png',                 'max-age=604800'),
    '/favicon.ico':   ('icon-192.png',  'image/png',                 'max-age=604800'),
    '/apple-touch-icon.png': ('icon-192.png', 'image/png',           'max-age=604800'),
}


def serve_static(cl, path):
    fn, ctype, cache = _STATIC[path]
    try:
        f = open(fn, 'rb')
    except Exception:
        ota.send(cl, 'not found', 'text/plain', '404 Not Found')
        return
    ota._send_all(cl, "HTTP/1.1 200 OK\r\nContent-Type: {}\r\n"
                      "Cache-Control: {}\r\nConnection: close\r\n\r\n".format(ctype, cache))
    try:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            ota._send_all(cl, chunk)
    finally:
        f.close()


def serve_page(cl):
    # Stream the UI from page.html on flash so the ~30KB page never sits in RAM
    # (that headroom is what WiFi needs). __CFG__ is substituted inline; a small
    # carry handles the token being split across two reads.
    ota._send_all(cl, "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
    cfg = _cfg_js()
    tok = "__CFG__"
    tl = len(tok)
    carry = ""
    try:
        f = open("page.html")
    except Exception:
        ota._send_all(cl, "<h1>page.html missing</h1>")
        return
    try:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            data = carry + chunk
            j = data.find(tok)
            while j >= 0:
                ota._send_all(cl, data[:j])
                ota._send_all(cl, cfg)
                data = data[j + tl:]
                j = data.find(tok)
            if len(data) > tl - 1:
                ota._send_all(cl, data[:-(tl - 1)])
                carry = data[-(tl - 1):]
            else:
                carry = data
        if carry:
            ota._send_all(cl, carry)
    finally:
        f.close()


def _stream(cl, s, a, b, step=1024):
    while a < b:
        e = a + step
        if e > b:
            e = b
        ota._send_all(cl, s[a:e])
        a = e


def serve_reading(cl):
    now = time.ticks_ms()
    age = time.ticks_diff(now, _v_ts) if _v_ts else 999999
    fresh = _v_ok and age < 2000
    sps = 0
    for t in _meas_t[:]:
        if time.ticks_diff(now, t) < 1000:
            sps += 1
    ota.send(cl, '{{"ok":{},"value":{},"raw":"{}","age":{},"sps":{}}}'.format(
        'true' if fresh else 'false', _fmt(_v_val), _v_raw, age, sps), "application/json")


def serve_status(cl):
    g = MODE_MAP.get(_func or _func_req, ('', ''))[1]
    global _mem_max
    gc.collect()              # report true usage (post-GC) + keep the heap defragged
    mf = gc.mem_free()
    ma = gc.mem_alloc()
    tot = mf + ma             # wobbles a few hundred B (GC segment accounting)
    if tot > _mem_max:        # high-water = stable ceiling, known since boot
        _mem_max = tot
    body = ('{{"idn":"{}","function":"{}","g":"{}","rate":"{}","range":"{}","auto":{},'
            '"value":{},"raw":"{}","memfree":{},"memalloc":{},"memmax":{},"follow":{},"fw":"{}"}}').format(
        _idn, _func or _func_req, g, _rate or '?', _range_lbl, _auto,
        _fmt(_v_val), _v_raw, mf, ma, _mem_max, 1 if _follow_dev else 0, CODE_TIMESTAMP)
    ota.send(cl, body, "application/json")


def serve_function(cl, q):
    global _func_req
    k = ota.qparam(q, "set")
    if k in MODE_MAP:
        _func_req = k
        ota.send(cl, '{{"ok":true,"function":"{}"}}'.format(k), "application/json")
    else:
        ota.send(cl, '{"ok":false,"err":"unknown"}', "application/json", "400 Bad Request")


def serve_rate(cl, q):
    global _rate_req
    r = ota.qparam(q, "set")
    if r in ('S', 'M', 'F'):
        _rate_req = r
        ota.send(cl, '{{"ok":true,"rate":"{}"}}'.format(r), "application/json")
    else:
        ota.send(cl, '{"ok":false,"err":"rate S|M|F"}', "application/json", "400 Bad Request")


def serve_range(cl, q):
    global _range_req
    s = ota.qparam(q, "set")
    if s in ('auto', 'up', 'down'):
        _range_req = s
        ota.send(cl, '{{"ok":true,"range":"{}"}}'.format(s), "application/json")
    else:
        ota.send(cl, '{"ok":false,"err":"auto|up|down"}', "application/json", "400 Bad Request")


def rpc(cmd):
    """Send ONE SCPI command to the meter via the poller (lock-serialized so
    web /api/scpi and the SCPI-TCP server don't collide on the UART)."""
    global _rpc_cmd
    if _rpc_lock:
        _rpc_lock.acquire()
    try:
        seq0 = _rpc_seq
        _rpc_cmd = cmd
        deadline = time.ticks_add(time.ticks_ms(), 2500)
        while _rpc_seq == seq0 and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(10)
        return _rpc_resp if _rpc_seq != seq0 else '(timeout)'
    finally:
        if _rpc_lock:
            _rpc_lock.release()


def serve_scpi(cl, q):
    scpi = ota.qparam(q, "cmd") or ota.qparam(q, "scpi")
    scpi = scpi.replace("+", " ").replace("%20", " ").replace("%3F", "?").replace("%3f", "?")
    if not scpi:
        ota.send(cl, '{"ok":false,"err":"no cmd"}', "application/json", "400 Bad Request")
        return
    resp = rpc(scpi)
    ota.send(cl, '{{"sent":"{}","resp":"{}"}}'.format(scpi, resp), "application/json")


def _sys_exec(code):
    """Run a MicroPython snippet from the console /exec command, capturing print()."""
    import machine
    buf = []

    def _p(*a, **k):
        buf.append(k.get('sep', ' ').join(str(x) for x in a))

    g = {'print': _p, 'gc': gc, 'os': os, 'time': time, 'network': network,
         'machine': machine, 'ubinascii': ubinascii}
    try:
        r = eval(code, g)                      # expression → return its value
        out = "\n".join(buf)
        if r is not None:
            out = (out + "\n" if out else "") + repr(r)
        return out or "(ok)"
    except SyntaxError:
        try:
            exec(code, g)                      # statements → capture prints
            return "\n".join(buf) or "(ok)"
        except Exception as e:
            return "ERR " + repr(e)
    except Exception as e:
        return "ERR " + repr(e)


def serve_sys(cl, method, body):
    """ESP-level '/' commands typed in the console (POST body holds the raw command)."""
    import json
    try:
        cmd = body.decode() if isinstance(body, (bytes, bytearray)) else (body or '')
    except Exception:
        cmd = ''
    cmd = cmd.strip()
    if not cmd.startswith('/'):
        ota.send(cl, json.dumps({"resp": "ERR not a / command"}), "application/json")
        return
    p = cmd[1:].split(' ', 1)
    op = p[0].lower()
    arg = p[1].strip() if len(p) > 1 else ''
    resp = ''
    try:
        if op == 'help':
            resp = ("/help  /mem  /df  /files  /ip  /uptime  /gc  /reboot  "
                    "/factory yes  /exec <python>\n(no leading slash = SCPI to the meter)")
        elif op == 'mem':
            gc.collect()
            free, alloc = gc.mem_free(), gc.mem_alloc()
            resp = "free {} B   alloc {} B   total {} B".format(free, alloc, free + alloc)
        elif op == 'df':
            s = os.statvfs('/')
            total, free = s[0] * s[2], s[0] * s[3]
            resp = "flash  free {} KB / total {} KB".format(free // 1024, total // 1024)
        elif op == 'files':
            rows = []
            for fn in sorted(os.listdir()):
                try:
                    st = os.stat(fn)
                    rows.append(fn + "/" if st[0] & 0x4000 else "{}   {} B".format(fn, st[6]))
                except Exception:
                    rows.append(fn)
            resp = "\n".join(rows)
        elif op == 'ip':
            try:
                sta = network.WLAN(network.STA_IF)
                ip = sta.ifconfig()[0]
                mac = ubinascii.hexlify(sta.config('mac'), ':').decode()
            except Exception:
                ip, mac = '?', '?'
            try:
                host = network.hostname()
            except Exception:
                host = '?'
            resp = "ip {}   host {}.local   mac {}".format(ip, host, mac)
        elif op == 'uptime':
            sec = time.ticks_diff(time.ticks_ms(), _BOOT_TICKS) // 1000
            resp = "up {}h {}m {}s".format(sec // 3600, (sec % 3600) // 60, sec % 60)
        elif op == 'gc':
            before = gc.mem_free()
            gc.collect()
            after = gc.mem_free()
            resp = "freed {} B   (free {} B)".format(after - before, after)
        elif op == 'reboot':
            ota.send(cl, json.dumps({"resp": "(rebooting…)"}), "application/json")
            try:
                cl.close()
            except Exception:
                pass
            import machine
            time.sleep_ms(300)
            machine.reset()
            return
        elif op == 'factory':
            if arg != 'yes':
                resp = "type  /factory yes  to confirm (wipes WiFi + network, reboots to setup AP)"
            else:
                ota.send(cl, json.dumps({"resp": "(factory reset…)"}), "application/json")
                try:
                    cl.close()
                except Exception:
                    pass
                import machine
                for fn in ('wifi.dat', 'net.dat'):
                    try:
                        os.remove(fn)
                    except Exception:
                        pass
                time.sleep_ms(300)
                machine.reset()
                return
        elif op == 'exec':
            resp = _sys_exec(arg) if arg else "usage: /exec <python>   e.g. /exec print(gc.mem_free())"
        else:
            resp = "unknown: /{}   (try /help)".format(op)
    except Exception as e:
        resp = "ERR " + repr(e)
    ota.send(cl, json.dumps({"resp": resp}), "application/json")


def scpi_server():
    """Raw SCPI-TCP server on port 5025 for PyVISA (TCPIP::host::5025::SOCKET).
    Line-based ('<cmd>\\n'); a command ending in '?' returns a reply, else write."""
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', 5025))
        s.listen(1)
        s.settimeout(1.0)
    except Exception as e:
        log('SCPI', 'bind err {}'.format(e))
        return
    log('SCPI', 'TCP-Server :5025 (PyVISA)')
    while True:
        wd.feed()
        try:
            cl, addr = s.accept()
        except OSError:
            continue
        try:
            cl.settimeout(2.0)
            buf = b''
            alive = True
            while alive:
                wd.feed()
                try:
                    chunk = cl.recv(256)
                except OSError as er:
                    if er.args and er.args[0] == 116:   # ETIMEDOUT -> keep connection open
                        continue
                    break
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    cmd = line.decode().strip()
                    if not cmd:
                        continue
                    if cmd.endswith('?'):
                        try:
                            cl.send((rpc(cmd) + '\n').encode())
                        except Exception:
                            alive = False
                            break
                    else:
                        rpc(cmd)
        except Exception as e:
            log('SCPI', 'err {}'.format(e))
        finally:
            try:
                cl.close()
            except Exception:
                pass


def web_server(ip):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(3)
    s.settimeout(1.0)
    log('WEB', 'http://{}/'.format(ip))
    while True:
        wd.feed()
        try:
            cl, addr = s.accept()
        except OSError:
            gc.collect()
            continue
        try:
            cl.settimeout(8.0)
            method, path, q, body = ota.read_request(cl)
            if ota.handle(cl, method, path, q, body):
                pass
            elif path == '/api/reading':
                serve_reading(cl)
            elif path == '/api/status':
                serve_status(cl)
            elif path == '/api/function':
                serve_function(cl, q)
            elif path == '/api/rate':
                serve_rate(cl, q)
            elif path == '/api/range':
                serve_range(cl, q)
            elif path in _STATIC:
                serve_static(cl, path)
            elif path == '/api/net':
                if ota.qparam(q, 'dhcp') or ota.qparam(q, 'ip'):
                    serve_net_set(cl, q)
                else:
                    serve_net_get(cl)
            elif path == '/api/devsync':
                serve_devsync(cl, q)
            elif path == '/api/factory':
                serve_factory(cl, q)
            elif path == '/api/crash':
                serve_crash(cl, q)
            elif path in ('/api/scpi', '/cmd'):
                serve_scpi(cl, q)
            elif path == '/sys':
                serve_sys(cl, method, body)
            else:
                serve_page(cl)
        except Exception as e:
            log('WEB', 'req err {}'.format(e))
        finally:
            try:
                cl.close()
            except Exception:
                pass


# ─── Entry ────────────────────────────────────────────────────────────────────────

def run():
    global tx_pin, rx_pin, led, _follow_dev
    _follow_dev = read_follow()
    tx_pin = Pin(TX_PIN, Pin.IN)
    rx_pin = Pin(RX_PIN, Pin.IN)
    led = Pin(LED_PIN, Pin.OUT)
    led.on()

    log('SYS', '== OWON XDM Remote ==')
    try:
        mac = ubinascii.hexlify(network.WLAN().config('mac'), ':').decode()
    except Exception:
        mac = ubinascii.hexlify(network.WLAN().config('mac')).decode()
    log('SYS', 'MAC {}'.format(mac))
    log('SYS', 'Code {}'.format(CODE_TIMESTAMP))
    gc.collect()
    log('SYS', 'Heap {}'.format(gc.mem_free()))

    net = read_net()
    try:
        network.hostname(net['host'])
        log('WIFI', 'hostname {}'.format(net['host']))
    except Exception as e:
        log('WIFI', 'hostname err {}'.format(e))
    log('WIFI', 'WifiManager')
    WifiManager(ssid='OWON-XDM-Remote-Setup', password='', reboot=False, debug=True).connect()
    if (not net['dhcp']) and net['ip']:
        try:
            network.WLAN(network.STA_IF).ifconfig((net['ip'], net['mask'], net['gw'], net['dns']))
            log('WIFI', 'static IP {}'.format(net['ip']))
        except Exception as e:
            log('WIFI', 'static err {}'.format(e))
    ip = network.WLAN(network.STA_IF).ifconfig()[0]
    log('WIFI', 'IP {}'.format(ip))
    wd.feed()

    run_sequence()
    wd.feed()

    if _thread is not None:
        try:
            _thread.start_new_thread(poller, ())
        except Exception as e:
            log('POLL', 'thread fail {}'.format(e))
        try:
            _thread.start_new_thread(scpi_server, ())
        except Exception as e:
            log('SCPI', 'thread fail {}'.format(e))
    log('RUN', 'open http://{}/  · SCPI :5025'.format(ip))
    web_server(ip)
