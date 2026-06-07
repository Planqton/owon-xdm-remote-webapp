# OWON XDM1041 Remote Bench 🌡️🔌

WiFi remote & web interface for the **OWON XDM1041** bench multimeter, running on an **ESP32-WROOM** (MicroPython) — with a professional browser dashboard, live graph, OTA updates, web console, and **brick-proof self-healing**.

> **Derivative work / fork of [Elektroarzt/owon-xdm-remote](https://github.com/Elektroarzt/owon-xdm-remote) — licensed under GPLv3.**
> The original (ESP32-C3 + MQTT) was the basis: `firmware/wifi_manager.py` comes from there (Igor Ferreira, mod. Elektroarzt), as does the SCPI startup handshake. This variant runs on a classic **ESP32-WROOM**, **without MQTT/Home Assistant** — instead a standalone web app, OTA and watchdog/rollback.

> 🚧 **Early development / work in progress.** This project is at an early stage. Expect rough edges and bugs — several features don't work as intended yet and things may change or break. Use at your own risk and feel free to open issues.

![Dashboard](docs/screenshot.png)

---

## ✨ Features

- **Full-window browser dashboard** — uses the whole window, dark instrument theme, responsive (its own phone layout).
- **All measurement modes** as tabs: **V** (DC/AC), **A** (DC/AC), **Ω** (resistance / continuity / diode), **capacitance**, **frequency**, **temperature**.
- **Side toolbar**: sampling rate (Low/Mid/High), range (auto / manual ▲▼), trigger (auto/single), hold.
- **Live graph with axes** (value & time axis), **min/max/avg**, and an **"FPS-like" sample-rate readout** (samples/s).
- **Unit display**: "match meter" (unit follows the range, like the display) or auto-scale (mV/V/kV, µA/mA/A, Ω/kΩ/MΩ, pF/nF/µF, Hz/kHz/MHz, °C); **"OL"** on overload.
- **Settings** in the browser: **3 themes** (Midnight/Carbon/Light) + fully customizable colors with **export/import**, **network** (hostname, DHCP/static IP), **display** (unit mode), **system** info + **Factory reset**.
- **OTA updates** over WiFi — works in **every** state (app, recovery, setup-AP).
- **Web console** (`/console`) — live log in the browser, including crash traceback. No USB needed for debugging.
- **Brick-proof**: hardware watchdog + boot counter + full-set "golden snapshot" auto-rollback.
- **mDNS** (`owon.local`), captive-portal WiFi setup with auto-redirect.

---

## 🔌 Hardware & wiring

| Part | Value |
|---|---|
| MCU | ESP32-WROOM-32 (ESP32-D0WD-V3, 4 MB flash) |
| Meter | OWON XDM1041 |
| Firmware | MicroPython 1.28.0 (ESP32_GENERIC) |

Remove the original internal UART/USB board inside the OWON and connect the ESP to the same header (J3). Wire **crossed over**, **3.3 V logic — no level shifter needed**:

| OWON (J1) | → | ESP32-WROOM |
|---|---|---|
| **TXD** (OWON transmits) | → | **GPIO16** (ESP RX) |
| **RxD** (OWON receives) | → | **GPIO17** (ESP TX) |
| **V_IN** (5 V) | → | **VIN / 5V** |
| **GND** | → | **GND** |

> ⚠️ Do **not** feed OWON 5 V (V_IN) **and** ESP USB at the same time (no protection diode). When flashing over USB, disconnect the V_IN line. In normal operation: OWON 5 V only.

KiCad PCB and assets are in `production/` and `assets/` (from upstream — same hardware).

---

## ⚡ Quick start

Everything runs through the interactive helper **`flash.sh`** (whiptail TUI):

```bash
./flash.sh
```

### 1. First setup (over USB)
1. Connect the ESP over USB (disconnect OWON 5 V / V_IN).
2. `./flash.sh` → **First flash via USB**.
   - Installs `esptool`/`mpremote` if needed, flashes MicroPython and uploads all firmware files (incl. the rollback baseline in `good/`).
3. Then: join the open WiFi **`OWON-XDM-Remote-Setup`**, open **`http://192.168.4.1`**, enter your WiFi.
4. The page auto-redirects to **`http://owon.local/`**. Done. 🎉

### 2. Updates (over OTA, no USB)
1. `./flash.sh` → **OTA update**.
   - Finds **all** ESPs on the network (mDNS + subnet scan) — even in recovery/AP mode after an aborted update.
   - With **multiple devices** you get a **selection list** (IP, mode, hostname, serial) so you pick the right one.
2. Choose files (all / app.py only / individual) → upload → reboot.

---

## 🖥️ Usage

### WiFi setup (first run / captive portal)

On first boot — or after a factory reset, or whenever it can't reach a saved network — the ESP opens its own access point and waits for you to configure WiFi:

1. Wait ~15 s for the ESP to boot.
2. On your phone/PC, open the WiFi list and connect to the **open** network **`OWON-XDM-Remote-Setup`**.
3. The setup page usually opens automatically (captive portal). If not, open **`http://192.168.4.1`** in a browser.
4. In the portal:
   - pick your WiFi from the scanned list (use the **↻ Refresh** button to rescan),
   - enter the **password** twice (with **Show password** to verify it),
   - tap **Save & Connect**.
5. A **"Saved"** page appears with a spinner. Now **reconnect your phone/PC to your normal WiFi** (the `OWON-XDM-Remote-Setup` AP disappears once the ESP joins your network).
6. As soon as you're back on your network, the page **automatically redirects to `http://owon.local/`**. Done. 🎉

> The WiFi credentials are stored on the ESP, so it reconnects on its own after a reboot/power-cycle. You can change the network later in **Settings → System → Factory reset** (which re-opens the setup portal), or just re-flash.

### Daily use

Open **`http://owon.local/`** (or your hostname).

- **Tabs at the top**: pick the measurement function, sub-modes underneath.
- **Toolbar on the right**: sampling, range, trigger, hold.
- **Gear** (top right): settings (Design, Display, Network, System).
- **Log** / **OTA**: web console and update page.

---

## 🌐 Hostname / mDNS

- Default: **`owon.local`**. Change it under **Settings → Network → Hostname** → **"Save & Reboot"** (takes effect after a reboot). The page auto-redirects to the new name.
- The **`.local`** suffix is fixed (mDNS). `owon.me`/`.com` etc. are not possible.
- A static IP is also configurable (otherwise DHCP). Worst case the device is always reachable by its IP.

---

## 🛡️ Reliability (brick-proof)

A bad update — crash, syntax error **or full hang** — self-heals **without USB** (A/B style):

1. **Watchdog** (armed early in `main.py`, fed in every loop): a hang → reset.
2. **Boot counter** counts failed boots.
3. **Self-confirm**: after ~14 s of stable running the app saves the **entire app set** as a known-good snapshot in **`good/`** and clears the counters.
4. **Full-set auto-rollback**: after 4 failed boots, `main.py` restores the **whole `good/` set** and **reboots straight back into the repaired app** (loop guard: after 2 unsuccessful attempts → recovery).

> **Trust root** is only `main.py` + `wd.py` (tiny, stable) — these are updated via USB (like a bootloader). Everything else self-heals via `good/`.
> Tested: a broken `ota.py` (kills app **and** recovery) pushed over OTA → back to the normal app in ~35 s, fully automatic, no USB. ✅
>
> To disable the watchdog for USB debugging, create a `nowdt` file on the ESP, then delete it.

---

## 🔧 HTTP API

| Route | Purpose |
|---|---|
| `GET /api/reading` | current reading `{ok,value,raw,age,sps}` |
| `GET /api/status` | full state (function, rate, range, IDN …) |
| `GET /api/function?set=VDC\|VAC\|ADC\|AAC\|RES\|CONT\|DIOD\|CAP\|FREQ\|TEMP` | switch function |
| `GET /api/rate?set=S\|M\|F` | sampling Low/Mid/High |
| `GET /api/range?set=auto\|up\|down` | range |
| `GET /api/net` · `?host=&dhcp=&ip=&mask=&gw=&dns=` | network read/set |
| `GET /api/factory?confirm=1` | factory reset (wipe WiFi+network, reboot to setup AP) |
| `GET /api/scpi?cmd=...` | arbitrary SCPI (debug) |
| `POST /upload?name=X` · `GET /ota` · `POST /reboot` | OTA |
| `GET /console` · `GET /log` · `POST /logclear` | web console |

---

## 📜 Credits & license

This project is a **derivative work** of **[Elektroarzt/owon-xdm-remote](https://github.com/Elektroarzt/owon-xdm-remote)** and is therefore — like the original — licensed under the **GNU General Public License v3 (GPLv3)**, see [`LICENSE`](LICENSE).

- **Original & basis:** Elektroarzt/owon-xdm-remote (hardware concept, `wifi_manager.py`, SCPI startup handshake).
- `wifi_manager.py`: WiFiManager by **Igor Ferreira** (MIT), modified by Elektroarzt and extended here.
- **New in this variant:** ESP32-WROOM port, full web app/REST API, OTA + recovery + watchdog + full-set rollback, `flash.sh`.

When distributing/publishing: comply with GPLv3 (open source + attribution).
