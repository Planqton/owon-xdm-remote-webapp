#####################################################################################
# ota.py - minimal OTA + web-console helpers, shared by app.py and recovery.py
#
# Routes handled by handle():
#   GET  /ota            -> upload page (file list + picker + reboot)
#   POST /upload?name=X  -> write request body to file X (raw body, no multipart)
#   GET/POST /reboot     -> machine.reset()
#   GET  /console        -> live log console page
#   GET  /log            -> plain-text log buffer
#   POST /logclear       -> clear the log buffer
#
# Upload from a PC:
#   curl -X POST --data-binary @app.py "http://<ip>/upload?name=app.py"
#####################################################################################

import os
import time
import machine
import dbg

CRLF2 = b"\r\n\r\n"
STREAM_TMP = "ota_tmp.bin"
STREAM_THRESHOLD = 2048
_STREAM_PATH = None   # set by read_request when a large body was streamed to file


def read_request(cl, maxhdr=4096):
    """Read an HTTP request. Returns (method, path, query, body_bytes).

    Large POST bodies (> STREAM_THRESHOLD) are streamed straight to a temp file
    instead of buffered in RAM (avoids MemoryError on big OTA uploads); body is
    then returned empty and _STREAM_PATH points at the temp file.
    """
    global _STREAM_PATH
    _STREAM_PATH = None
    data = b""
    while CRLF2 not in data:
        chunk = cl.recv(256)
        if not chunk:
            break
        data += chunk
        if len(data) > maxhdr:
            break
    if CRLF2 in data:
        head, body = data.split(CRLF2, 1)
    else:
        head, body = data, b""
    lines = head.split(b"\r\n")
    try:
        method, target, _ = lines[0].split(b" ", 2)
    except Exception:
        method, target = b"GET", b"/"
    path, _, query = target.partition(b"?")
    clen = 0
    for ln in lines[1:]:
        if ln.lower().startswith(b"content-length:"):
            try:
                clen = int(ln.split(b":", 1)[1].strip())
            except Exception:
                clen = 0
            break
    if b"expect: 100-continue" in head.lower():
        try:
            cl.send(b"HTTP/1.1 100 Continue\r\n\r\n")
        except Exception:
            pass
    if clen > STREAM_THRESHOLD:
        try:
            f = open(STREAM_TMP, "wb")
            if body:
                f.write(body)
            got = len(body)
            while got < clen:
                chunk = cl.recv(1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
            f.close()
            _STREAM_PATH = STREAM_TMP
        except Exception as e:
            dbg.log("OTA", "stream err {}".format(e))
        return method.decode(), path.decode(), query.decode(), b""
    while len(body) < clen:
        chunk = cl.recv(1024)
        if not chunk:
            break
        body += chunk
    return method.decode(), path.decode(), query.decode(), body


def qparam(query, key):
    for part in query.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k == key:
                return v
    return ""


def list_files():
    out = []
    try:
        for f in os.listdir():
            try:
                st = os.stat(f)
                if st[0] & 0x4000:
                    continue
                out.append((f, st[6]))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(out)


def _send_all(cl, data):
    if isinstance(data, str):
        data = data.encode()
    try:
        cl.sendall(data)
        return
    except AttributeError:
        pass
    mv = memoryview(data)
    n = len(data)
    off = 0
    while off < n:
        try:
            sent = cl.send(mv[off:])
        except OSError:
            break
        if not sent:
            break
        off += sent


def send(cl, body, ctype="text/html; charset=utf-8", status="200 OK"):
    _send_all(cl, "HTTP/1.1 {}\r\nContent-Type: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".format(status, ctype))
    _send_all(cl, body)


_NAV = '<p style="margin-top:6px"><a class="lnk" href="/">&larr; Meter</a> &nbsp; <a class="lnk" href="/ota">OTA</a> &nbsp; <a class="lnk" href="/console">Console</a></p>'

_CSS = """*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;
background:#0f172a;color:#e2e8f0;margin:0;padding:24px;display:flex;justify-content:center}
.card{background:#1e293b;border-radius:18px;box-shadow:0 10px 40px rgba(0,0,0,.4);padding:24px 28px;max-width:760px;width:100%}
h1{font-size:22px;margin:0 0 4px}.sub{color:#94a3b8;font-size:13px;margin-bottom:18px}
ul{list-style:none;padding:0;margin:0 0 18px;border:1px solid #334155;border-radius:12px;overflow:hidden}
li{display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #273449;font-variant-numeric:tabular-nums}
li:last-child{border-bottom:0}li span{color:#64748b;font-size:12px}
input[type=file]{width:100%;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:10px;color:#e2e8f0;margin-bottom:12px}
.btn{padding:10px 15px;border:0;border-radius:10px;font-weight:700;cursor:pointer;margin:0 8px 0 0}
.up{background:#2563eb;color:#fff}.rb{background:#7f1d1d;color:#fecaca}.cl{background:#334155;color:#e2e8f0}
.lnk{color:#38bdf8;text-decoration:none;font-size:13px}.lnk:hover{text-decoration:underline}
#msg{margin-top:14px;font-size:13px;color:#94a3b8;word-break:break-all}
pre{background:#0b1220;border:1px solid #334155;border-radius:12px;padding:14px;height:60vh;overflow:auto;
margin:0 0 14px;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;color:#cbd5e1}"""


_OTA_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OTA Update</title>
<style>{{CSS}}</style></head><body><div class="card">
<h1>OTA Update</h1><div class="sub">{{SUB}}</div>
<ul>{{FILES}}</ul>
<input type="file" id="f">
<button class="btn up" onclick="up()">Upload</button>
<button class="btn rb" onclick="rb()">Reboot</button>
<div id="msg"></div>
{{NAV}}
<script>
async function up(){var f=document.getElementById('f').files[0];
 if(!f){msg('Please choose a file.');return;}msg('Uploading '+f.name+'…');
 try{var r=await fetch('/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});var j=await r.json();
  msg(j.ok?('OK: '+j.name+' ('+j.size+' B). Press Reboot to apply.'):('Error: '+j.err));
  if(j.ok)setTimeout(function(){location.reload()},800);
 }catch(e){msg('Upload failed: '+e);}}
async function rb(){msg('Rebooting…');try{await fetch('/reboot',{method:'POST'});}catch(e){}}
function msg(t){document.getElementById('msg').textContent=t;}
</script></div></body></html>"""


_CONSOLE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Console</title>
<style>{{CSS}}</style></head><body><div class="card">
<h1>Console</h1><div class="sub">Live log (RAM ring buffer). Auto-refreshing.</div>
<pre id="log">…</pre>
<button class="btn cl" onclick="clr()">Clear</button>
<button class="btn rb" onclick="rb()">Reboot</button>
<label style="font-size:13px;color:#94a3b8;margin-left:10px"><input type="checkbox" id="auto" checked> Auto-Scroll</label>
{{NAV}}
<script>
var pre=document.getElementById('log');
async function upd(){try{var r=await fetch('/log',{cache:'no-store'});var t=await r.text();
 var atBottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
 pre.textContent=t||'(empty)';
 if(document.getElementById('auto').checked||atBottom)pre.scrollTop=pre.scrollHeight;
 }catch(e){pre.textContent='Console unreachable: '+e;}}
async function clr(){try{await fetch('/logclear',{method:'POST'});upd();}catch(e){}}
async function rb(){try{await fetch('/reboot',{method:'POST'});}catch(e){}}
setInterval(upd,1000);upd();
</script></div></body></html>"""


def page(cl, sub="Choose a file and upload, then reboot.", note=""):
    rows = "".join("<li>{} <span>{} B</span></li>".format(n, s) for n, s in list_files())
    html = (_OTA_PAGE.replace("{{CSS}}", _CSS).replace("{{FILES}}", rows)
            .replace("{{SUB}}", sub).replace("{{NAV}}", _NAV))
    send(cl, html)


def console(cl):
    send(cl, _CONSOLE_PAGE.replace("{{CSS}}", _CSS).replace("{{NAV}}", _NAV))


def logtext(cl):
    send(cl, dbg.text(), "text/plain; charset=utf-8")


def logclear(cl):
    dbg.clear()
    send(cl, '{"ok":true}', "application/json")


def upload(cl, query, body):
    name = qparam(query, "name")
    if (not name) or ("/" in name) or name in (".", ".."):
        send(cl, '{"ok":false,"err":"bad filename"}', "application/json", "400 Bad Request")
        return
    try:
        if _STREAM_PATH:
            try:
                os.remove(name)
            except Exception:
                pass
            os.rename(_STREAM_PATH, name)
            size = os.stat(name)[6]
        else:
            with open(name, "wb") as f:
                f.write(body)
            size = len(body)
        dbg.log("OTA", "wrote {} ({} B)".format(name, size))
        send(cl, '{{"ok":true,"name":"{}","size":{}}}'.format(name, size), "application/json")
    except Exception as e:
        send(cl, '{{"ok":false,"err":"{}"}}'.format(e), "application/json", "500 Internal Server Error")


def reboot(cl):
    send(cl, '{"ok":true}', "application/json")
    try:
        cl.close()
    except Exception:
        pass
    dbg.log("OTA", "reboot requested")
    time.sleep_ms(300)
    machine.reset()


def handle(cl, method, path, query, body):
    """Handle OTA/console routes. Returns True if the request was handled."""
    if path == "/ota":
        page(cl); return True
    if path == "/upload" and method == "POST":
        upload(cl, query, body); return True
    if path == "/console":
        console(cl); return True
    if path == "/log":
        logtext(cl); return True
    if path == "/logclear" and method == "POST":
        logclear(cl); return True
    if path == "/reboot":
        reboot(cl); return True
    return False
