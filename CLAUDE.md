# OWON XDM1041 Remote – Projektnotizen

Stand: 2026-06-07. Wissensspeicher für nahtloses Weitermachen.

## ✅ AKTUELLER STAND: LÄUFT
- Gerät erreichbar unter **`http://owon.local/`** (Name bleibt stabil, egal welche IP DHCP vergibt – getestet, funktioniert via avahi/mDNS).
- Aktuelle Firmware: `app v5: net settings`, voll funktionsfähig: Pro-UI (Tabs, Toolbar, Graph mit Achsen, Min/Max/Avg, FPS-Anzeige), `/api`, OTA, Web-Konsole, Settings (Design-Themes + Netzwerk), **brick-sicheres OTA mit Auto-Rollback + Watchdog**.
- IP variiert per DHCP (zuletzt `192.168.0.85`) – **IP-Suche unnötig, einfach `owon.local`**.

### Was die früheren „Hänger/kaputt" wirklich waren (gelöst)
1. **DHCP-IP-Wechsel** (.86→.85): sah aus wie „Webserver tot", war nur falsche IP. → Lösung: `owon.local`.
2. **MemoryError**: die ~22-KB-Seite wurde pro Request via `PAGE.replace(...)` neu alloziert → bei knappem/fragmentiertem Heap Absturz, der einmal App **und** Recovery mitriss (REPL-Hang, brauchte USB). → Lösung: Seite wird in **1-KB-Häppchen gestreamt** (`app.serve_page`/`_stream`), nie mehr große Allokation. `ota.send` nutzt `_send_all`/`sendall`.

## 🛡️ Brick-sicheres OTA (FERTIG & END-TO-END GETESTET ✅)
Ein kaputtes Update – Crash, Syntaxfehler ODER Totalhänger – repariert sich **vollständig ohne USB**:
- **Watchdog FRÜH** in `main.py` via `wd.py`: `import wd; wd.start(30000)` als allererstes (vor `import app`). Gefüttert mit `wd.feed()` in JEDER Dauerschleife: app-Poller, app-Webserver, recovery-Server, WLAN-Portal, WLAN-Connect-Loop, Startup (`wait_ready`/`set_rate_high`). → Ein Hänger IRGENDWO (auch im Import/Startup oder REPL-Absturz) → Reset nach 30 s.
- **Boot-Zähler** `boot.cnt`: `main.py` erhöht ihn jeden Boot.
- **Self-Confirm**: läuft die App ~14 s stabil (40 Poller-Durchläufe), sichert sie den **kompletten App-Satz als Snapshot nach `/good/`** (`dbg.py, ota.py, recovery.py, wifi_manager.py, app.py`) und setzt `boot.cnt=0`+`restored.cnt=0` (`app._mark_healthy`).
- **Voll-Set Auto-Rollback (A/B-Prinzip)**: bei **4 Fehlboots/Resets in Folge** stellt `main.py` den **ganzen `good/`-Satz** wieder her (nicht nur app.py!), und **bootet automatisch zurück in die reparierte App** (`machine.reset()`). Schleifen-Schutz: nach max. 2 erfolglosen Restores → Recovery. `restored.cnt` zählt das.
- **Trust-Root = `main.py` + `wd.py`** (winzig, stabil): NICHT rollback-geschützt → **nur per USB** aktualisieren. Alles andere ist via `good/` selbstheilend. (Wie der Bootloader beim Handy.) `flash.sh` warnt bei OTA dieser Dateien.
- **GETESTET (2026-06-07), beides ohne USB:** (a) hängendes `app.py` → WDT-Resets → Rollback → zurück. (b) **kaputtes `ota.py`** (legt App UND Recovery lahm) per OTA → Boot-Loop → `boot-loop (4) -> good/ wiederhergestellt (5 Dateien)` → **automatischer Reboot → normale App in ~35 s**. ✅
- **Recovery** (`recovery.py`) + **Streaming-OTA** (`ota.py`) als interaktiver Fallback, falls kein `good/`-Snapshot existiert.
- ⚠️ **WDT beim USB-Debuggen abschalten:** Datei `nowdt` auf dem ESP anlegen (`mpremote connect /dev/ttyUSB0 exec "open('nowdt','w').close()"`), sonst resettet der WDT alle 30 s im REPL. Danach wieder löschen + Reset.
- Ablauf eines kaputten OTA-Updates: ~4×(Boot+30 s) ≈ 2–3 Min unbeaufsichtigt → wieder online. Während der Zyklen ist das Gerät kurz weg; danach kommt es allein zurück (über `owon.local`/DHCP-IP).

## 🔁 OTA ist IMMER verfügbar (NEU)
Die OTA-Endpunkte (`/upload`, `/ota`, `/reboot`, `/console`, `/log`) hängen jetzt auch am **Captive-Portal** (`wifi_manager.web_server` nutzt `ota.read_request`/`ota.handle`). Also OTA in ALLEN Zuständen: Normalbetrieb, Recovery UND Setup-AP-Modus (`http://192.168.4.1/upload?name=...`). Man kann nie mehr in einen Zustand ohne OTA geraten.

## ⚙️ Setup-Portal (verbessert)
Captive-Portal (`wifi_manager.handle_root`): WLAN-Passwort **+ Bestätigungsfeld (2×, JS prüft Gleichheit)**, **„Passwort anzeigen"**-Checkbox, **„↻ Aktualisieren"**-Button (rescannt SSIDs via `location.reload()`). Erfolgsseite leitet automatisch zu `<host>.local` weiter.

## 🌐 Hostname / owon.local
- **Umbenennen funktioniert (mDNS), getestet** – z. B. `owon` → `owon1` → `owon1.local`. Settings → Netzwerk → Hostname → **„Speichern & Neustart"** (nur „Nur speichern" allein übernimmt NICHT – Hostname gilt erst nach Reboot, wird in `app.run()` via `network.hostname()` vor dem WLAN-Connect gesetzt).
- ⚠️ Tippfehler-Falle: `owonn1` (doppeltes n) → dann ist der Name `owonn1.local`. Genau auf Schreibweise achten. Notfalls immer über die IP erreichbar.
- Der **Name** (`owon`) ist **frei änderbar**: Settings → Zahnrad → Tab **Netzwerk** → Feld „Hostname" (z. B. `werkstatt` → dann `werkstatt.local`). Gilt nach Neustart.
- Das **`.local` ist FIX** (mDNS-Standard). **`owon.me`/`.com` o. ä. gehen NICHT** – das wären echte Internet-Domains, kein lokales mDNS.
- mDNS-Responder ist in MicroPython 1.28 ESP32 aktiv, sobald `network.hostname(...)` vor dem WLAN-Connect gesetzt wird (macht `app.run()`).

## Netzwerk-Einstellungen (Settings → Netzwerk)
- Hostname, Modus **DHCP/Feste IP**, IP/Maske/Gateway/DNS. Persistiert in **`net.dat`**, gilt nach Neustart.
- API: `GET /api/net` (lesen), `GET /api/net?host=..&dhcp=0|1&ip=..&mask=..&gw=..&dns=..` (setzen).
- ⚠️ Feste IP nur außerhalb des DHCP-Pools wählen, sonst Konflikt. **Empfehlung: DHCP lassen + `owon.local`** (kein Brick-Risiko). Eine falsche feste IP bräuchte sonst USB.
- **Captive-Portal-Auto-Redirect (NEU):** Nach der WLAN-Einrichtung zeigt die „Gespeichert"-Seite einen Spinner und **leitet automatisch zu `<host>.local` weiter**, sobald erreichbar. Hinweis dort: Handy/PC zurück ins Heim-WLAN verbinden → dann springt die Seite selbst auf den Bench. (In `wifi_manager.handle_configure`.)

## Hardware / Verkabelung
- ESP32-WROOM-32 (ESP32-D0WD-V3, 4 MB), MAC `28:05:a5:6f:4c:cc`. MicroPython 1.28.0 @ `0x1000`.
- OWON XDM1041, IDN `OWON,XDM1041,25240645,V4.3.0,3`.
- UART2, **TX=GPIO17→OWON RxD, RX=GPIO16←OWON TXD** (über Kreuz), **V_IN(5V)→VIN**, GND→GND. 3,3 V Logik, kein Pegelwandler.
- ⚠️ OWON-5V und ESP-USB nicht gleichzeitig (keine SS14-Diode). Zum USB-Flashen V_IN abziehen.

## 🧪 PyVISA / SCPI-TCP (Port 5025) – GETESTET ✅
`app.scpi_server()` = eigener Thread (parallel zu Poller+Web). Roher SCPI-Socket auf **5025**, zeilenbasiert `\n`; endet der Befehl auf `?` → Antwort, sonst Write. Geht über denselben `rpc()`-Mechanismus wie `/api/scpi`, jetzt mit `_rpc_lock` serialisiert (Web + TCP gleichzeitig sicher). `poll_once` RPC: Query (`?`) wartet auf Antwort, Write nur `send_cmd` (kein Timeout). PC: `pip install --user --break-system-packages pyvisa pyvisa-py`, dann `rm=pyvisa.ResourceManager('@py'); inst=rm.open_resource('TCPIP::owon1.local::5025::SOCKET')` + `read/write_termination='\n'`. Testscript: **`pyvisa_test.py`** im Projektroot (read-only; `--switch` für Schreib-Demo). Getestet: `*IDN?/FUNC?/RANGE?/RATE?/MEAS?` über PyVISA, parallel zur Web-UI.

## SCPI (verifiziert)
- `MEAS?` lesen (~200–300 ms → max ~3–5/s, Hardware-Limit). Kein Streaming. `VAL1?`/`FETC?` nicht unterstützt.
- **`1E+9` = Overload/offen** → UI zeigt „OL", raus aus Graph/Statistik (Schwelle ≥1e9).
- Funktionen: `CONF:VOLT:DC|AC`, `CONF:CURR:DC|AC`, `CONF:RES`, `CONF:CONT`, `CONF:DIOD`, `CONF:CAP`, `CONF:FREQ`, `CONF:TEMP`. `FUNC?` bestätigt.
- Sampling: `RATE S|M|F` (Low/Mid/High), `RATE?`.
- Range: `RANGE 1..6` (manuell, schaltet AUTO aus), `AUTO 1` (Autorange), `RANGE?`→Label, `AUTO?`→0/1. DCV: 1=50mV…6=1000V. **„500 V" ist der BEREICH, nicht die Einheit** – kleiner Wert wird trotzdem in mV angezeigt; bei zu großem Bereich rauscht es → „Auto" drücken.
- Kein SCPI zum Reboot/Aus des OWON (`*RST` = nur Settings-Reset). ESP kann nur sich selbst neu starten.

## Architektur / Dateien (auf dem ESP & in `firmware/`)
- `main.py` – Launcher: Boot-Zähler, Auto-Rollback (≥4 Fehlboots → `app_good.py` zurück), sonst `app.run()`, bei Crash → `recovery.run()` mit Traceback ins Log.
- `app.py` – App (~43 KB): WLAN(+hostname/static IP), Startup-Handshake, **Poller-Thread** (UART-Owner, je Zyklus EINE Änderung Funktion/Rate/Range, dann `MEAS?`; füttert WDT; ruft `_mark_healthy`), Webserver (Seite **gestreamt**), `/api`, Themes, Netzwerk-Settings, WDT.
- `recovery.py` – Notfall-Server (WLAN + OTA + Konsole), memory-arm.
- `ota.py` – OTA + Konsole. **`_send_all`/`sendall`** (kein Truncate), Upload **streamt >2 KB in Temp-Datei** (`ota_tmp.bin`, kein RAM-Buffer), `100-continue`.
- `dbg.py` – `dbg.log()` → seriell + 250-Zeilen-RAM-Ring für `/console`.
- `wd.py` – gemeinsamer Hardware-Watchdog: `wd.start(ms)` (in `main.py`, respektiert `nowdt`-Datei), `wd.feed()` (in allen Schleifen).
- Laufzeit-Dateien: `app_good.py` (Rollback-Basis), `boot.cnt`, `net.dat`, `wifi.dat`, `boot.py`.
- **`firmware/` = Quelle der Wahrheit.** `/tmp/owon/code` ist flüchtig (PC-Neustart löscht es!).

## API
`/` Seite · `/api/reading` {ok,value,raw,age,sps} · `/api/status` · `/api/function?set=KEY` · `/api/rate?set=S|M|F` · `/api/range?set=auto|up|down` · `/api/net` (get/set) · `/api/scpi?cmd=...` (Debug, auch `/cmd`) · `/ota` `/upload?name=X` `/reboot` · `/console` `/log` `/logclear`.

## OTA-Deploy (so geht's, kein USB nötig)
```bash
curl -s --max-time 40 -H "Expect:" -X POST --data-binary @app.py "http://owon.local/upload?name=app.py"
curl -s -H "Expect:" -X POST "http://owon.local/reboot"
```
- **IMMER `-H "Expect:"`** (sonst 100-continue-Stall).
- Große Dateien ok (Streaming). Antwort soll `{"ok":true,...,"size":N}` sein.
- Nach Reboot ~15 s warten, dann `curl http://owon.local/api/status`. Dank Auto-Rollback ist ein kaputtes Update nicht mehr fatal.

## Helfer-Skript & Doku (NEU)
- **`flash.sh`** (Projektwurzel, interaktives Menü): **1) Erstflash per USB** (MicroPython + alle `firmware/`-Dateien + `app_good.py`-Seed + `boot.cnt=0`), **2) OTA-Update** (scannt Netz, findet ALLE ESPs auch im Recovery/AP-Modus via `/ota`-Endpoint, **Auswahl-Liste bei mehreren Geräten** mit IP/Modus/Host/IDN, lädt auf gewählte IP), **3) ESPs suchen**. Merkt sich letzten Host in `.owon_last_host`. Subnetz wird aus `ip route` erkannt.
- **`README.md`** = schöne GitHub-Doku.

## Tooling
- `esptool`, `mpremote` (pip `--user --break-system-packages`). Port `/dev/ttyUSB0`, User in `dialout`.
- USB belegt? `fuser -k -9 /dev/ttyUSB0`. (NICHT `pkill -f mpremote` in einem Befehl, der selbst „mpremote" enthält → killt sich selbst.)
- Datei per USB: `python3 -m mpremote connect /dev/ttyUSB0 fs cp f.py :f.py`. Reset: `... exec "import machine;machine.reset()"`.
- Gerät im Netz finden (falls owon.local mal klemmt): MAC im ARP: `ip neigh | grep -i 28:05:a5`.

## Gelernte Lektionen
1. Bei diesem ESP **nichts in einem großen Stück allozieren** – Seiten/Antworten **streamen** (Heap fragmentiert schnell mit Poller-Thread + großen String-Konstanten).
2. `cl.send()` macht Partial-Sends → `sendall`/Schleife nötig.
3. curl braucht `-H "Expect:"`.
4. OTA-Update am OTA-Code (`ota.py`/`app.py`) ist riskant → jetzt durch Auto-Rollback + WDT abgesichert.
5. DHCP-IP wechselt → `owon.local` nutzen.
6. Memory-Pointer `[[owon-xdm-esp32-setup]]`.

## TODO / Ideen
- (Optional) Mehr Settings-Tabs, Datenlogging/CSV-Export, UI-Feinschliff.
- (Optional) Feste-IP-Pfad real testen (bisher nur Schreib-/Lesepfad getestet; Anwenden bei Boot ist implementiert, aber risikobehaftet ohne USB).
