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
    ('Ω', [('RES', 'Widerstand'), ('CONT', 'Durchgang'), ('DIOD', 'Diode')]),
    ('Cap', [('CAP', 'Kapazitaet')]),
    ('Hz',  [('FREQ', 'Frequenz')]),
    ('°C', [('TEMP', 'Temperatur')]),
]
DEFAULT_MODE = 'VDC'

CODE_TIMESTAMP = "2026-06-06 (app v9: SCPI server :5025)"

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
    global _rpc_cmd, _rpc_resp, _rpc_seq

    # 1) RPC (von /api/scpi und vom SCPI-TCP-Server): Query wartet auf Antwort,
    #    Write (ohne '?') wird nur gesendet -> kein 1s-Timeout.
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


SNAP_FILES = ('dbg.py', 'ota.py', 'recovery.py', 'wifi_manager.py', 'app.py')


def _mark_healthy():
    """Stabiler Lauf bestätigt: Boot-Zähler nullen und einen kompletten
    bekannt-guten Snapshot des App-Satzes nach /good/ sichern, damit der
    Launcher nach einem kaputten OTA-Update ALLES wiederherstellen kann."""
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
        log('BOOT', 'healthy: Snapshot good/ ({} Dateien)'.format(cnt))
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


PAGE = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>OWON XDM1041 Bench</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0f1a;--panel:#121a2b;--panel2:#0e1626;--line:#243049;--mut:#64748b;--txt:#e2e8f0;--acc:#38bdf8;--accb:#2563eb;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,Arial,sans-serif;background:var(--bg);color:var(--txt);display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;background:var(--panel2);border-bottom:1px solid var(--line);flex:0 0 auto}
.brand{font-weight:800;letter-spacing:.5px}.brand span{font-weight:400;color:var(--mut);font-size:12px;margin-left:8px}
.hlinks a{color:var(--mut);text-decoration:none;font-size:13px;margin-left:14px}.hlinks a:hover{color:var(--acc)}
.sps{font-variant-numeric:tabular-nums;font-size:12px;color:var(--ok);background:#0e1a12;border:1px solid #1f3b29;border-radius:6px;padding:2px 8px}
.conn{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mut);margin-left:14px}
.conn.on{background:var(--ok)}.conn.bad{background:var(--bad)}
.tabs{display:flex;gap:4px;padding:8px 12px;background:var(--panel2);border-bottom:1px solid var(--line);flex:0 0 auto;overflow-x:auto}
.tab{padding:9px 20px;border:1px solid var(--line);border-bottom:none;border-radius:10px 10px 0 0;background:transparent;color:var(--mut);font-weight:700;font-size:15px;cursor:pointer;white-space:nowrap}
.tab.active{background:var(--panel);color:var(--acc);border-color:var(--acc)}
.main{flex:1 1 auto;display:grid;grid-template-columns:1fr 300px;min-height:0}
.stage{display:flex;flex-direction:column;padding:18px 22px;min-width:0}
.submodes{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.sub{padding:7px 16px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--mut);font-size:13px;font-weight:600;cursor:pointer}
.sub.active{background:var(--accb);border-color:var(--accb);color:#fff}
.readout{flex:1 1 auto;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--panel);border:1px solid var(--line);border-radius:16px;min-height:0;position:relative}
.reading{display:flex;align-items:baseline}
.val{font-size:clamp(48px,11vw,140px);font-weight:800;font-variant-numeric:tabular-nums;color:var(--acc);line-height:1}
.val.hold{color:var(--warn)}.val.ol{color:var(--bad)}
.unit{font-size:clamp(20px,3vw,42px);color:var(--mut);margin-left:10px}
.modeline{margin-top:10px;color:var(--mut);font-size:14px;letter-spacing:1px}
.spark{width:96%;height:150px;margin-top:14px}
.stats{display:flex;gap:22px;margin-top:10px;color:var(--mut);font-size:13px;align-items:center}
.stats b{color:var(--txt);font-variant-numeric:tabular-nums}
.stats button{background:var(--panel2);color:var(--mut);border:1px solid var(--line);border-radius:8px;padding:4px 10px;cursor:pointer;font-size:12px}
.panel{background:var(--panel2);border-left:1px solid var(--line);padding:16px;overflow:auto;display:flex;flex-direction:column;gap:18px}
.grp h3{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--mut);margin-bottom:8px}
.seg{display:flex;gap:6px}
.seg button,.wide,.rangebox button{font-family:inherit}
.seg button{flex:1;padding:10px 0;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--mut);font-weight:700;cursor:pointer}
.seg button.active{background:var(--accb);border-color:var(--accb);color:#fff}
.rangebox{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.rangebox button{width:46px;padding:10px 0;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--txt);font-size:16px;cursor:pointer}
.rangebox span{flex:1;text-align:center;font-weight:700;font-variant-numeric:tabular-nums}
.wide{width:100%;padding:11px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--txt);font-weight:700;cursor:pointer}
.wide.active{background:var(--accb);border-color:var(--accb);color:#fff}
.hold.active{background:var(--warn);border-color:var(--warn);color:#1a1206}
button:disabled{opacity:.4;cursor:not-allowed}
#gear{font-size:16px;text-decoration:none;color:var(--mut);margin-left:14px}#gear:hover{color:var(--acc)}
.ovl{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:50}
.ovl.show{display:flex}
.wait{position:fixed;inset:0;background:rgba(8,12,22,.94);display:none;align-items:center;justify-content:center;z-index:100;text-align:center}
.wait.show{display:flex}
.wait h2{color:var(--acc);margin:16px 0 6px}.wait p{color:var(--mut);max-width:440px;margin:0 auto}
.wait .sp{width:46px;height:46px;border:4px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:spin 1s linear infinite;margin:0 auto}
.wait a{display:inline-block;margin-top:18px;background:var(--accb);color:#fff;text-decoration:none;padding:11px 20px;border-radius:10px;font-weight:700}
@keyframes spin{to{transform:rotate(360deg)}}
.dlg{background:var(--panel);border:1px solid var(--line);border-radius:16px;width:min(740px,95vw);max-height:92vh;overflow:auto}
.dlgtitle{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
.dlgtitle b{font-size:17px}.dlgx{cursor:pointer;color:var(--mut);font-size:22px;line-height:1}
.stabs{display:flex;gap:4px;padding:14px 16px 0}
.stab{padding:8px 18px;border:1px solid var(--line);border-bottom:none;border-radius:9px 9px 0 0;background:transparent;color:var(--mut);font-weight:700;cursor:pointer}
.stab.active{background:var(--panel2);color:var(--acc);border-color:var(--acc)}
.spane{padding:18px;display:none}.spane.active{display:block}
.themerow{display:flex;gap:10px;margin-bottom:18px}
.themebtn{flex:1;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--txt);cursor:pointer;font-weight:700}
.themebtn.active{border-color:var(--acc);color:var(--acc)}
.colors{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.colorrow{display:flex;align-items:center;justify-content:space-between;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px 12px}
.colorrow label{font-size:13px;color:var(--mut)}
.colorrow input{width:46px;height:30px;border:1px solid var(--line);border-radius:6px;background:none;cursor:pointer;padding:0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.actions button{padding:9px 14px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--txt);cursor:pointer;font-weight:600}
.actions button.primary{background:var(--accb);border-color:var(--accb);color:#fff}
.io{width:100%;height:96px;margin-top:10px;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:9px;font-family:monospace;font-size:12px;padding:8px;display:none}
.sysinfo{font-size:13px;line-height:1.9;color:var(--mut)}.sysinfo b{color:var(--txt)}
.netgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.netgrid label{display:flex;flex-direction:column;font-size:12px;color:var(--mut);gap:5px}
.netgrid input,.netgrid select{padding:8px 10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--txt);font-size:13px}
.hint{font-size:12px;color:var(--mut);margin:10px 0 0;line-height:1.5}
@media (max-width:760px){
 .main{grid-template-columns:1fr;grid-template-rows:1fr auto}
 .panel{border-left:none;border-top:1px solid var(--line);flex-direction:row;flex-wrap:wrap;gap:12px;max-height:42vh}
 .grp{flex:1 1 130px}
 .stage{padding:12px}
 .spark{height:96px}
 .stats{gap:14px;font-size:12px;flex-wrap:wrap;justify-content:center}
}
</style></head><body>
<header>
 <div class="brand">OWON XDM1041 <span id="idn">verbinde…</span></div>
 <div class="hlinks"><span id="sps" class="sps">– S/s</span><a href="#" id="gear" title="Einstellungen">&#9881;</a><a href="/console">Console</a><a href="/ota">OTA</a><span id="conn" class="conn"></span></div>
</header>
<nav class="tabs" id="tabs"></nav>
<div class="main">
 <section class="stage">
  <div class="submodes" id="submodes"></div>
  <div class="readout">
   <div class="reading"><span id="val" class="val">--</span><span id="unit" class="unit"></span></div>
   <div class="modeline" id="modeline"></div>
   <canvas id="spark" class="spark"></canvas>
   <div class="stats">
    <span>MIN <b id="min">–</b></span><span>MAX <b id="max">–</b></span><span>AVG <b id="avg">–</b></span>
    <button id="rstStat">Reset</button>
   </div>
  </div>
 </section>
 <aside class="panel">
  <div class="grp"><h3>Sampling</h3><div class="seg" id="rate">
   <button data-r="S">Low</button><button data-r="M">Mid</button><button data-r="F">High</button></div></div>
  <div class="grp"><h3>Range</h3>
   <div class="rangebox"><button id="rdown">&#9660;</button><span id="rangelbl">–</span><button id="rup">&#9650;</button></div>
   <button id="rauto" class="wide">Auto</button></div>
  <div class="grp"><h3>Trigger</h3><div class="seg" id="trig">
   <button data-t="auto" class="active">Auto</button><button data-t="single">Single</button></div>
   <button id="trigbtn" class="wide" disabled style="margin-top:8px">Trigger &#9658;</button></div>
  <div class="grp"><h3>Anzeige</h3><button id="hold" class="wide hold">Hold</button></div>
 </aside>
</div>
<div class="ovl" id="ovl"><div class="dlg">
 <div class="dlgtitle"><b>Einstellungen</b><span class="dlgx" id="ovlx">&#10005;</span></div>
 <div class="stabs"><button class="stab active" data-s="design">Design</button><button class="stab" data-s="display">Anzeige</button><button class="stab" data-s="network">Netzwerk</button><button class="stab" data-s="system">System</button></div>
 <div class="spane active" data-p="design">
  <div class="themerow" id="themerow"></div>
  <div class="colors" id="colors"></div>
  <div class="actions">
   <button class="primary" id="saveTheme">Speichern</button>
   <button id="resetTheme">Zuruecksetzen</button>
   <button id="expBtn">Export</button>
   <button id="impBtn">Import</button>
  </div>
  <textarea class="io" id="io" placeholder="JSON hier einfuegen, dann Uebernehmen"></textarea>
  <div class="actions" id="impActions" style="display:none"><button class="primary" id="impApply">Uebernehmen</button></div>
 </div>
 <div class="spane" data-p="network">
  <div class="netgrid">
   <label>Hostname<input id="n_host" placeholder="owon"></label>
   <label>Modus<select id="n_dhcp"><option value="1">DHCP (automatisch)</option><option value="0">Feste IP</option></select></label>
   <label>IP-Adresse<input id="n_ip" placeholder="192.168.0.50"></label>
   <label>Subnetzmaske<input id="n_mask" placeholder="255.255.255.0"></label>
   <label>Gateway<input id="n_gw" placeholder="192.168.0.1"></label>
   <label>DNS<input id="n_dns" placeholder="192.168.0.1"></label>
  </div>
  <div class="actions"><button id="netSave">Nur speichern</button><button class="primary" id="netReboot">Speichern &amp; Neustart (&uuml;bernehmen)</button></div>
  <p class="hint" id="netmsg"></p>
  <p class="hint">Hostname wird im Router angezeigt; <b>owon.local</b> klappt je nach Router/Client. Feste IP ist garantiert &ndash; bitte eine freie Adresse au&szlig;erhalb des DHCP-Bereichs w&auml;hlen. &Auml;nderungen gelten nach Neustart.</p>
 </div>
 <div class="spane" data-p="display">
  <div class="grp"><h3>Einheit</h3><div class="seg" id="umode">
   <button data-u="range">Wie Ger&auml;t</button><button data-u="auto">Auto-Skalierung</button></div></div>
  <p class="hint">&bdquo;Wie Ger&auml;t&ldquo;: Einheit folgt dem Messbereich (z.&nbsp;B. Bereich 5&nbsp;V &rarr; Anzeige in V) &ndash; wie das Multimeter-Display.<br>&bdquo;Auto-Skalierung&ldquo;: sch&ouml;nste Einheit je nach Wert (mV/V/kV &hellip;).</p>
 </div>
 <div class="spane" data-p="system"><div id="sysinfo" class="sysinfo">…</div></div>
</div></div>
<div class="wait" id="waitovl"><div><div class="sp"></div><h2 id="waitt">Neustart...</h2><p id="waitm"></p><a id="waitlink" href="#">&#246;ffnen</a></div></div>
<script>
var CFG=__CFG__;
function waitRedirect(url,title){
 el('waitt').textContent=title||'Neustart...';
 el('waitm').textContent='Warte, bis '+url+' erreichbar ist - dann automatische Weiterleitung.';
 var a=el('waitlink');a.href=url;a.textContent=url;
 el('waitovl').classList.add('show');
 setTimeout(function poll(){
  fetch(url+'api/status',{mode:'no-cors',cache:'no-store'}).then(function(){location.replace(url);}).catch(function(){setTimeout(poll,2000);});
 },6000);
}
var cur={k:'VDC',g:'V'},trig='auto',hold=false,curTab=0,curRange='',unitMode=(localStorage.getItem('xdm_unitmode')||'range');
var stats={min:null,max:null,sum:0,cnt:0},hist=[];
function runit(label){var m=(label||'').match(/([a-zA-ZµΩ°]+)$/);if(!m)return null;var u=m[1];
 var base=['V','A','Ω','F','Hz','C','°C','s'];var pf={p:1e12,n:1e9,u:1e6,'µ':1e6,m:1e3,k:1e-3,K:1e-3,M:1e-6,G:1e-9};
 if(base.indexOf(u)>=0)return{f:1,u:u};var c=u.charAt(0),rest=u.slice(1);
 if((c in pf)&&base.indexOf(rest)>=0)return{f:pf[c],u:u};return null;}
function fmt(v,g){if(v===null)return{n:'--',u:''};var a=Math.abs(v);if(a>=1e9)return{n:'OL',u:''};
 if(unitMode=='range'){var ru=runit(curRange);if(ru){var d=v*ru.f,bb=Math.abs(d),dc=bb>=1000?1:bb>=100?2:bb>=10?3:4;return{n:d.toFixed(dc),u:ru.u};}}
 if(g=='V'){if(a<1)return{n:(v*1e3).toFixed(3),u:'mV'};if(a<1e3)return{n:v.toFixed(4),u:'V'};return{n:(v/1e3).toFixed(4),u:'kV'};}
 if(g=='D')return{n:v.toFixed(4),u:'V'};
 if(g=='A'){if(a<1e-3)return{n:(v*1e6).toFixed(2),u:'µA'};if(a<1)return{n:(v*1e3).toFixed(3),u:'mA'};return{n:v.toFixed(4),u:'A'};}
 if(g=='R'){if(a<1e3)return{n:v.toFixed(2),u:'Ω'};if(a<1e6)return{n:(v/1e3).toFixed(3),u:'kΩ'};return{n:(v/1e6).toFixed(4),u:'MΩ'};}
 if(g=='C'){if(a<1e-9)return{n:(v*1e12).toFixed(1),u:'pF'};if(a<1e-6)return{n:(v*1e9).toFixed(2),u:'nF'};if(a<1e-3)return{n:(v*1e6).toFixed(3),u:'µF'};return{n:(v*1e3).toFixed(3),u:'mF'};}
 if(g=='Hz'){if(a<1e3)return{n:v.toFixed(2),u:'Hz'};if(a<1e6)return{n:(v/1e3).toFixed(3),u:'kHz'};return{n:(v/1e6).toFixed(4),u:'MHz'};}
 if(g=='T')return{n:v.toFixed(1),u:'°C'};
 return{n:String(v),u:''};}
function el(id){return document.getElementById(id);}
function buildTabs(){var t=el('tabs');t.innerHTML='';CFG.forEach(function(tb,i){
 var b=document.createElement('button');b.className='tab'+(i==curTab?' active':'');b.textContent=tb.n;
 b.onclick=function(){curTab=i;buildTabs();buildSubs();selSub(CFG[i].s[0]);};t.appendChild(b);});}
function buildSubs(){var s=el('submodes');s.innerHTML='';var subs=CFG[curTab].s;
 subs.forEach(function(sm){var b=document.createElement('button');b.className='sub'+(sm.k==cur.k?' active':'');
  b.textContent=sm.l;b.setAttribute('data-k',sm.k);b.onclick=function(){selSub(sm);};s.appendChild(b);});}
function selSub(sm){cur=sm;buildSubs();resetStats();fetch('/api/function?set='+sm.k,{cache:'no-store'});}
function resetStats(){stats={min:null,max:null,sum:0,cnt:0};hist=[];el('min').textContent='–';el('max').textContent='–';el('avg').textContent='–';}
function drawChart(){var c=el('spark');var w=c.width=c.clientWidth,h=c.height=c.clientHeight;var x=c.getContext('2d');
 var cs=getComputedStyle(document.documentElement),CL=(cs.getPropertyValue('--line')||'#243049').trim(),CM=(cs.getPropertyValue('--mut')||'#64748b').trim(),CA=(cs.getPropertyValue('--acc')||'#38bdf8').trim();
 x.clearRect(0,0,w,h);
 var PL=52,PR=10,PT=8,PB=18,pw=w-PL-PR,ph=h-PT-PB;
 x.font='11px system-ui';
 if(hist.length<2){x.strokeStyle=CL;x.strokeRect(PL,PT,pw,ph);return;}
 var vs=hist.map(function(p){return p.v;});
 var mn=Math.min.apply(null,vs),mx=Math.max.apply(null,vs);
 if(mx-mn<1e-12){var pad=Math.abs(mn)*0.05+1e-6;mn-=pad;mx+=pad;}
 var t1=hist[hist.length-1].t,t0=hist[0].t,span=Math.max(t1-t0,1);
 // horizontal grid + Y labels
 x.textAlign='right';x.textBaseline='middle';var N=4;
 for(var i=0;i<=N;i++){var fy=i/N,yy=PT+ph*fy,val=mx-(mx-mn)*fy;
  x.strokeStyle=CL;x.globalAlpha=.5;x.beginPath();x.moveTo(PL,yy);x.lineTo(PL+pw,yy);x.stroke();x.globalAlpha=1;
  x.fillStyle=CM;x.fillText(fmt(val,cur.g).n,PL-5,yy);}
 // vertical grid + time labels
 x.textAlign='center';x.textBaseline='top';var M=4;
 for(var k=0;k<=M;k++){var fx=k/M,xx=PL+pw*fx;
  x.strokeStyle=CL;x.globalAlpha=.5;x.beginPath();x.moveTo(xx,PT);x.lineTo(xx,PT+ph);x.stroke();x.globalAlpha=1;
  var sec=(t1-(t0+span*fx))/1000;
  x.fillStyle=CM;x.fillText(sec>0.5?('-'+sec.toFixed(0)+'s'):'jetzt',xx,PT+ph+4);}
 // unit (top-left) + border
 x.textAlign='left';x.textBaseline='top';x.fillStyle=CM;x.fillText(fmt(mx,cur.g).u,3,PT-1);
 x.strokeStyle=CL;x.strokeRect(PL,PT,pw,ph);
 // data line
 x.strokeStyle=CA;x.lineWidth=2;x.beginPath();
 hist.forEach(function(p,i){var px=PL+(p.t-t0)/span*pw,py=PT+ph-(p.v-mn)/(mx-mn)*ph;
  i?x.lineTo(px,py):x.moveTo(px,py);});x.stroke();}
function setReading(j){var f=fmt(j.value,cur.g);var ve=el('val');
 ve.textContent=f.n;el('unit').textContent=f.u;ve.className='val'+(hold?' hold':'')+(f.n=='OL'?' ol':'');
 if(j.value!==null&&Math.abs(j.value)<1e9){
  if(stats.min===null||j.value<stats.min)stats.min=j.value;
  if(stats.max===null||j.value>stats.max)stats.max=j.value;
  stats.sum+=j.value;stats.cnt++;hist.push({t:performance.now(),v:j.value});if(hist.length>200)hist.shift();
  el('min').textContent=fmt(stats.min,cur.g).n;el('max').textContent=fmt(stats.max,cur.g).n;
  el('avg').textContent=fmt(stats.sum/stats.cnt,cur.g).n;drawChart();}}
async function reading(){try{var r=await fetch('/api/reading',{cache:'no-store'});var j=await r.json();
 el('conn').className='conn on';if(!hold)setReading(j);
 if(j.sps!==undefined)el('sps').textContent=j.sps+' S/s';
 el('modeline').textContent=(j.ok?'live':'stale')+(j.raw?(' · '+j.raw):'');
 }catch(e){el('conn').className='conn bad';el('modeline').textContent='ESP nicht erreichbar';}}
async function status(){try{var r=await fetch('/api/status',{cache:'no-store'});var j=await r.json();
 el('idn').textContent=j.idn||'';el('rangelbl').textContent=j.range||'–';curRange=j.range||'';
 var bs=el('rate').children;for(var i=0;i<bs.length;i++)bs[i].classList.toggle('active',bs[i].getAttribute('data-r')==j.rate);
 el('rauto').classList.toggle('active',j.auto==1);
 }catch(e){}}
el('rate').onclick=function(e){var r=e.target.getAttribute('data-r');if(r)fetch('/api/rate?set='+r,{cache:'no-store'}).then(status);};
el('rup').onclick=function(){fetch('/api/range?set=up',{cache:'no-store'}).then(status);};
el('rdown').onclick=function(){fetch('/api/range?set=down',{cache:'no-store'}).then(status);};
el('rauto').onclick=function(){fetch('/api/range?set=auto',{cache:'no-store'}).then(status);};
el('trig').onclick=function(e){var t=e.target.getAttribute('data-t');if(!t)return;trig=t;
 var bs=el('trig').children;for(var i=0;i<bs.length;i++)bs[i].classList.toggle('active',bs[i].getAttribute('data-t')==t);
 el('trigbtn').disabled=(t!='single');};
el('trigbtn').onclick=function(){reading();};
el('hold').onclick=function(){hold=!hold;el('hold').classList.toggle('active',hold);};
el('rstStat').onclick=resetStats;
// ---- Themes / Settings ----
var VARS=['bg','panel','panel2','line','mut','txt','acc','accb','ok','warn','bad'];
var VLBL={bg:'Hintergrund',panel:'Panel',panel2:'Panel 2',line:'Linien',mut:'Sekundaer',txt:'Text',acc:'Akzent',accb:'Akzent 2',ok:'OK',warn:'Warnung',bad:'Fehler'};
var THEMES={
 Midnight:{bg:'#0a0f1a',panel:'#121a2b',panel2:'#0e1626',line:'#243049',mut:'#64748b',txt:'#e2e8f0',acc:'#38bdf8',accb:'#2563eb',ok:'#22c55e',warn:'#f59e0b',bad:'#ef4444'},
 Carbon:{bg:'#0d0d0f',panel:'#17181c',panel2:'#121316',line:'#2a2c33',mut:'#7a7d87',txt:'#e7e7ea',acc:'#f59e0b',accb:'#d97706',ok:'#22c55e',warn:'#eab308',bad:'#ef4444'},
 Hell:{bg:'#eef1f7',panel:'#ffffff',panel2:'#f6f8fc',line:'#d8dee9',mut:'#64748b',txt:'#0f172a',acc:'#2563eb',accb:'#2563eb',ok:'#16a34a',warn:'#d97706',bad:'#dc2626'}};
var palette=JSON.parse(JSON.stringify(THEMES.Midnight)),themeName='Midnight';
function applyPalette(p){VARS.forEach(function(k){document.documentElement.style.setProperty('--'+k,p[k]);});}
function loadTheme(){try{var t=localStorage.getItem('xdm_theme'),c=localStorage.getItem('xdm_palette');
 if(c){palette=JSON.parse(c);themeName=t||'Custom';}else if(t&&THEMES[t]){palette=JSON.parse(JSON.stringify(THEMES[t]));themeName=t;}}catch(e){}applyPalette(palette);}
function setTheme(n){themeName=n;palette=JSON.parse(JSON.stringify(THEMES[n]));applyPalette(palette);buildColors();buildThemeRow();}
function buildThemeRow(){var r=el('themerow');r.innerHTML='';Object.keys(THEMES).forEach(function(n){
 var b=document.createElement('button');b.className='themebtn'+(n==themeName?' active':'');b.textContent=n;b.onclick=function(){setTheme(n);};r.appendChild(b);});}
function buildColors(){var c=el('colors');c.innerHTML='';VARS.forEach(function(k){
 var row=document.createElement('div');row.className='colorrow';
 var lab=document.createElement('label');lab.textContent=VLBL[k];
 var inp=document.createElement('input');inp.type='color';inp.value=palette[k];
 inp.oninput=function(){palette[k]=inp.value;themeName='Custom';document.documentElement.style.setProperty('--'+k,inp.value);buildThemeRow();};
 row.appendChild(lab);row.appendChild(inp);c.appendChild(row);});}
function saveTheme(){try{localStorage.setItem('xdm_theme',themeName);localStorage.setItem('xdm_palette',JSON.stringify(palette));}catch(e){}}
function resetTheme(){try{localStorage.removeItem('xdm_theme');localStorage.removeItem('xdm_palette');}catch(e){}setTheme('Midnight');}
async function sysInfo(){try{var j=await(await fetch('/api/status',{cache:'no-store'})).json();
 el('sysinfo').innerHTML='Geraet: <b>'+j.idn+'</b><br>Firmware: <b>'+j.fw+'</b><br>Funktion: <b>'+j.function+'</b> · Rate: <b>'+j.rate+'</b> · Range: <b>'+j.range+'</b><br>Host: <b>'+location.host+'</b>';}catch(e){}}
function applyUmode(){var bs=el('umode').children;for(var i=0;i<bs.length;i++)bs[i].classList.toggle('active',bs[i].getAttribute('data-u')==unitMode);}
el('umode').onclick=function(e){var u=e.target.getAttribute('data-u');if(!u)return;unitMode=u;try{localStorage.setItem('xdm_unitmode',u);}catch(e){}applyUmode();};
el('gear').onclick=function(e){e.preventDefault();el('ovl').classList.add('show');buildThemeRow();buildColors();sysInfo();loadNet();applyUmode();};
el('ovlx').onclick=function(){el('ovl').classList.remove('show');};
el('ovl').onclick=function(e){if(e.target===el('ovl'))el('ovl').classList.remove('show');};
(function(){var ts=document.getElementsByClassName('stab');for(var i=0;i<ts.length;i++){ts[i].onclick=function(){
 var s=this.getAttribute('data-s');
 var a=document.getElementsByClassName('stab');for(var j=0;j<a.length;j++)a[j].classList.toggle('active',a[j].getAttribute('data-s')==s);
 var p=document.getElementsByClassName('spane');for(var k=0;k<p.length;k++)p[k].classList.toggle('active',p[k].getAttribute('data-p')==s);};}})();
el('saveTheme').onclick=saveTheme;el('resetTheme').onclick=resetTheme;
el('expBtn').onclick=function(){var io=el('io');io.style.display='block';io.value=JSON.stringify({theme:themeName,palette:palette});io.focus();io.select();el('impActions').style.display='none';};
el('impBtn').onclick=function(){var io=el('io');io.style.display='block';io.value='';io.focus();el('impActions').style.display='flex';};
el('impApply').onclick=function(){try{var o=JSON.parse(el('io').value);var p=o.palette||o;palette=p;themeName=o.theme||'Custom';applyPalette(palette);buildColors();buildThemeRow();saveTheme();el('io').style.display='none';el('impActions').style.display='none';}catch(e){alert('Ungueltiges JSON');}};
async function loadNet(){try{var j=await(await fetch('/api/net',{cache:'no-store'})).json();
 el('n_host').value=j.host||'';el('n_dhcp').value=String(j.dhcp);
 el('n_ip').value=j.ip||j.cur_ip||'';el('n_mask').value=j.mask||'';el('n_gw').value=j.gw||'';el('n_dns').value=j.dns||'';
 el('netmsg').textContent='Aktuelle IP: '+(j.cur_ip||'?');}catch(e){el('netmsg').textContent='Netz-Konfig nicht lesbar';}}
function netQ(){return 'host='+encodeURIComponent(el('n_host').value||'owon')+'&dhcp='+el('n_dhcp').value+'&ip='+encodeURIComponent(el('n_ip').value)+'&mask='+encodeURIComponent(el('n_mask').value)+'&gw='+encodeURIComponent(el('n_gw').value)+'&dns='+encodeURIComponent(el('n_dns').value);}
async function netSave(rb){try{var r=await(await fetch('/api/net?'+netQ(),{cache:'no-store'})).json();
 if(!r.ok){el('netmsg').textContent='Fehler: '+(r.err||'?');return;}
 if(rb){var host=(el('n_host').value||'owon').trim();var url='http://'+host+'.local/';waitRedirect(url,'Uebernehmen & Neustart...');try{await fetch('/reboot',{method:'POST'});}catch(e){}}
 else el('netmsg').textContent='Gespeichert. Aktiv nach Neustart.';
}catch(e){el('netmsg').textContent='Fehler: '+e;}}
el('netSave').onclick=function(){netSave(false);};
el('netReboot').onclick=function(){netSave(true);};
loadTheme();applyUmode();buildTabs();buildSubs();
setInterval(function(){if(trig=='auto'&&!hold)reading();},250);
setInterval(status,1500);status();reading();
</script></body></html>"""


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
    return cfg


def write_net(cfg):
    with open('net.dat', 'w') as f:
        for k in ('host', 'dhcp', 'ip', 'mask', 'gw', 'dns'):
            f.write('{}={}\n'.format(k, cfg.get(k, '')))


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
    for k in ('host', 'ip', 'mask', 'gw', 'dns'):
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


def serve_page(cl):
    # Stream the page in small chunks so we never allocate the whole ~22KB at once
    # (low/fragmented heap would otherwise MemoryError). __CFG__ is substituted inline.
    ota._send_all(cl, "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
    i = PAGE.find("__CFG__")
    if i < 0:
        _stream(cl, PAGE, 0, len(PAGE))
        return
    _stream(cl, PAGE, 0, i)
    ota._send_all(cl, _cfg_js())
    _stream(cl, PAGE, i + 7, len(PAGE))   # 7 = len("__CFG__")


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
    body = ('{{"idn":"{}","function":"{}","g":"{}","rate":"{}","range":"{}","auto":{},'
            '"value":{},"raw":"{}","fw":"{}"}}').format(
        _idn, _func or _func_req, g, _rate or '?', _range_lbl, _auto,
        _fmt(_v_val), _v_raw, CODE_TIMESTAMP)
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
    """Schickt EINEN SCPI-Befehl über den Poller ans Meter (Lock-serialisiert,
    damit Web /api/scpi und der SCPI-TCP-Server sich nicht in die Quere kommen)."""
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


def scpi_server():
    """Roher SCPI-TCP-Server auf Port 5025 fuer PyVISA (TCPIP::host::5025::SOCKET).
    Zeilenbasiert ('<cmd>\\n'); endet der Befehl auf '?' -> Antwort, sonst Write."""
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
                    if er.args and er.args[0] == 116:   # ETIMEDOUT -> Verbindung offen halten
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
            elif path == '/api/net':
                if ota.qparam(q, 'host') or ota.qparam(q, 'dhcp'):
                    serve_net_set(cl, q)
                else:
                    serve_net_get(cl)
            elif path in ('/api/scpi', '/cmd'):
                serve_scpi(cl, q)
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
    global tx_pin, rx_pin, led
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
