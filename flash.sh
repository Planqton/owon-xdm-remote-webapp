#!/usr/bin/env bash
#####################################################################################
# OWON XDM1041 Remote – flash & OTA helper (whiptail TUI)
#
#   Menu:
#     1) First flash via USB  (MicroPython + all firmware files)
#     2) OTA update           (network; finds ALL ESPs incl. recovery/AP mode,
#                              with device selection when there are several)
#     3) Find ESPs on the network
#
#   Firmware source: ./firmware/   (next to this script)
#####################################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW_DIR="$SCRIPT_DIR/firmware"
PORT="${PORT:-/dev/ttyUSB0}"
LAST_HOST_FILE="$SCRIPT_DIR/.owon_last_host"
BT="OWON XDM1041 Remote"

# App set = OTA-safe (good/ snapshot, auto-rollback)
APP_FILES=(dbg.py ota.py recovery.py wifi_manager.py page.html app.py)
# Bootloader / trust-root = USB only (not rollback-protected)
CORE_FILES=(wd.py main.py)
FILES=("${CORE_FILES[@]}" "${APP_FILES[@]}")

MPY_BIN="$SCRIPT_DIR/ESP32_GENERIC-v1.28.0.bin"
MPY_URL="https://micropython.org/resources/firmware/ESP32_GENERIC-20260406-v1.28.0.bin"

HAS_WT=0; command -v whiptail >/dev/null 2>&1 && HAS_WT=1
[ "${OWON_NOTUI:-0}" = "1" ] && HAS_WT=0   # force plain-text prompts (scripting/CI)

DEVICES=()

# ── UI helpers (whiptail; text fallback if not present) ───────────────────────────
ui_menu(){ local t="$1" p="$2"; shift 2
  if [ "$HAS_WT" = 1 ]; then
    whiptail --backtitle "$BT" --title "$t" --menu "$p" 20 76 12 "$@" 3>&1 1>&2 2>&3
  else
    { echo; echo "== $t =="; echo "$p"; while [ $# -ge 2 ]; do printf "  %s) %s\n" "$1" "$2"; shift 2; done; } >&2
    local c; read -rp "> " c; echo "$c"
  fi
}
ui_check(){ local t="$1" p="$2"; shift 2
  if [ "$HAS_WT" = 1 ]; then
    whiptail --backtitle "$BT" --title "$t" --checklist "$p" 20 76 12 "$@" 3>&1 1>&2 2>&3
  else
    { echo "== $t =="; } >&2; local out=""
    while [ $# -ge 3 ]; do read -rp "$1 ? (y/N) " a >&2; [[ "${a:-}" =~ ^[yY] ]] && out="$out \"$1\""; shift 3; done
    echo "$out"
  fi
}
ui_msg(){ if [ "$HAS_WT" = 1 ]; then whiptail --backtitle "$BT" --title "$1" --msgbox "$2" 18 76
  else echo; echo "== $1 =="; echo -e "$2"; read -rp "[Enter]" _; fi; }
ui_yesno(){ if [ "$HAS_WT" = 1 ]; then whiptail --backtitle "$BT" --title "$1" --yesno "$2" 14 76
  else read -rp "$2 (y/N) " a; [[ "${a:-}" =~ ^[yY] ]]; fi; }

mp(){ python3 -m mpremote connect "$PORT" "$@"; }

need_tools(){
  command -v python3 >/dev/null 2>&1 || return 1
  command -v curl    >/dev/null 2>&1 || return 1
  if ! python3 -m mpremote version >/dev/null 2>&1; then
    echo "Installing mpremote/esptool ..."
    python3 -m pip install --user --break-system-packages mpremote esptool >/dev/null 2>&1 \
      || python3 -m pip install mpremote esptool
  fi
}

# ── ESP detection (universal: /ota exists in app, recovery AND AP portal) ─────────
is_esp(){ local t="${2:-3}"
  # fast: tiny JSON (app mode); fallback: /ota page (recovery/AP mode)
  curl -s --max-time "$t" "http://$1/api/reading" 2>/dev/null | grep -q '"ok"' && return 0
  curl -s --max-time "$t" "http://$1/ota" 2>/dev/null | grep -q "OTA Update"
}

get_ip(){
  local ip
  ip="$(curl -s --max-time 2 "http://$1/api/net" 2>/dev/null | grep -oE '"cur_ip":"[0-9.]+"' | grep -oE '[0-9.]+' | head -n1)"
  [ -z "$ip" ] && ip="$(getent hosts "$1" 2>/dev/null | awk '{print $1; exit}')"
  [ -z "$ip" ] && ip="$1"
  echo "$ip"
}

info_str(){
  local s host mode idn
  s="$(curl -s --max-time 2 "http://$1/api/status" 2>/dev/null)"
  if echo "$s" | grep -q "XDM1041"; then mode="App"; idn="$(echo "$s" | grep -oE '"idn":"[^"]*"' | sed 's/.*"idn":"//;s/"$//')"; else mode="Recovery/Setup"; idn=""; fi
  host="$(curl -s --max-time 2 "http://$1/api/net" 2>/dev/null | grep -oE '"host":"[^"]*"' | sed 's/.*"host":"//;s/"$//')"
  printf '%s | %s | %s' "$mode" "${host:+$host.local}" "${idn:-?}"
}

subnet_base(){
  local ip; ip="$(ip route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | awk '{print $2}')"
  [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$ip" ] && { echo "192.168.0"; return; }
  echo "${ip%.*}"
}

collect_devices(){
  DEVICES=(); local seen=" " h ip base tmp n
  local cands=(); [ -f "$LAST_HOST_FILE" ] && cands+=("$(cat "$LAST_HOST_FILE")")
  cands+=(owon.local owon1.local 192.168.4.1)
  for h in "${cands[@]}"; do [ -n "$h" ] || continue
    if is_esp "$h" 4; then ip="$(get_ip "$h")"; case "$seen" in *" $ip "*) ;; *) DEVICES+=("$ip"); seen="$seen$ip ";; esac; fi
  done
  base="$(subnet_base)"; echo "Scanning ${base}.1-254 ..."
  tmp="$(mktemp)"
  for n in $(seq 1 254); do ( is_esp "${base}.${n}" 2 && echo "${base}.${n}" >> "$tmp" ) & (( n % 60 == 0 )) && wait; done
  wait
  while read -r ip; do [ -n "$ip" ] || continue; case "$seen" in *" $ip "*) ;; *) DEVICES+=("$ip"); seen="$seen$ip ";; esac; done < <(sort -t. -k4 -n "$tmp" 2>/dev/null)
  rm -f "$tmp"
}

flash_mpy(){
  [ -f "$MPY_BIN" ] || { echo "Downloading MicroPython v1.28.0 ..."; curl -fSL -o "$MPY_BIN" "$MPY_URL" || return 1; }
  echo "Erasing flash ..."; python3 -m esptool --port "$PORT" erase-flash || return 1
  echo "Writing MicroPython @0x1000 ..."; python3 -m esptool --port "$PORT" --baud 460800 write-flash 0x1000 "$MPY_BIN" || return 1
}

# ── 1) First flash via USB ────────────────────────────────────────────────────────
usb_flash(){
  need_tools || { ui_msg "Error" "python3 or curl missing."; return; }
  if [ ! -e "$PORT" ]; then
    ui_msg "USB" "No $PORT found.\n\n- Connect USB to the ESP, disconnect OWON 5V/V_IN.\n- Different port:  PORT=/dev/ttyUSB1 ./flash.sh"; return
  fi
  ui_yesno "First flash via USB" "Flash MicroPython (if needed) and upload all firmware files to $PORT?\n\nIMPORTANT: OWON 5V/V_IN disconnected?" || return
  clear; echo "### First flash via USB ($PORT) ###"
  local doflash=0
  if mp exec "print(1)" >/dev/null 2>&1; then
    ui_yesno "MicroPython" "MicroPython is already installed.\n\nRe-flash it?" && doflash=1
  else doflash=1; fi
  if [ "$doflash" = 1 ]; then clear; echo "### Flashing MicroPython ###"; flash_mpy || { ui_msg "Error" "MicroPython flash failed (see terminal)."; return; }; sleep 2; fi
  clear; echo "### Uploading files ###"; local f okc=0
  for f in "${FILES[@]}"; do
    [ -f "$FW_DIR/$f" ] || { echo "MISSING: $f"; continue; }
    mp fs cp "$FW_DIR/$f" ":$f" >/dev/null 2>&1 && { echo "  ok $f"; okc=$((okc+1)); } || echo "  ERROR $f"
  done
  echo "Creating good/ snapshot ..."
  mp exec "import os
try:
    os.mkdir('good')
except Exception:
    pass" >/dev/null 2>&1
  for f in "${APP_FILES[@]}"; do mp fs cp "$FW_DIR/$f" ":good/$f" >/dev/null 2>&1; done
  mp exec "open('boot.cnt','w').write('0')" >/dev/null 2>&1
  mp exec "import machine;machine.reset()" >/dev/null 2>&1 || true
  ui_msg "Done" "$okc/${#FILES[@]} files flashed + good/ snapshot, reset.\n\nNext step:\n  WiFi 'OWON-XDM-Remote-Setup' -> http://192.168.4.1\n  -> set up your WiFi -> http://owon.local/"
}

# ── 2) OTA update ─────────────────────────────────────────────────────────────────
ota_update(){
  need_tools || { ui_msg "Error" "python3 or curl missing."; return; }
  clear; echo "### Searching for ESPs on the network ###"
  collect_devices
  local n="${#DEVICES[@]}" host d
  if [ "$n" = 0 ]; then ui_msg "OTA" "No ESP found.\n\n- powered on & on the same network?\n- in setup mode: connect to WiFi 'OWON-XDM-Remote-Setup' first, then retry."; return; fi
  if [ "$n" = 1 ]; then host="${DEVICES[0]}"; ui_yesno "1 device found" "$host\n[$(info_str "$host")]\n\nUse this device?" || return
  else
    local args=(); for d in "${DEVICES[@]}"; do args+=("$d" "$(info_str "$d")"); done
    host="$(ui_menu "Select device ($n found)" "Which ESP should be flashed?" "${args[@]}")" || return
    [ -z "$host" ] && return
  fi
  echo "$host" > "$LAST_HOST_FILE"
  local choice sel=()
  choice="$(ui_menu "Files" "What to upload to $host?" \
    "all"  "Complete app set (recommended, OTA-safe)" \
    "app"  "app.py only" \
    "pick" "Select individually (app set)" \
    "core" "Bootloader main.py/wd.py (CAUTION!)")" || return
  case "$choice" in
    all) sel=("${APP_FILES[@]}");;
    app) sel=(app.py);;
    pick) local cargs=(); for d in "${APP_FILES[@]}"; do cargs+=("$d" "" off); done
          local res; res="$(ui_check "Select files" "Space = on/off, Enter = OK" "${cargs[@]}")" || return
          eval "sel=($res)";;
    core) ui_yesno "CAUTION – bootloader" "main.py/wd.py are the trust root (bootloader).\nA broken update here can BRICK the ESP (recoverable only via USB).\n\nReally flash the bootloader over OTA?" || return; sel=("${CORE_FILES[@]}");;
    *) return;;
  esac
  [ "${#sel[@]}" = 0 ] && { ui_msg "OTA" "Nothing selected."; return; }
  clear; echo "### OTA upload -> $host ###"; local f r out="" fail=0
  for f in "${sel[@]}"; do
    [ -f "$FW_DIR/$f" ] || { echo "MISSING: $f"; out="$out\n  MISSING $f"; fail=1; continue; }
    printf "Upload %-18s " "$f ..."
    r="$(curl -s --max-time 90 -H "Expect:" -X POST --data-binary @"$FW_DIR/$f" "http://$host/upload?name=$f" 2>/dev/null)"
    if echo "$r" | grep -q '"ok":true'; then echo "ok"; out="$out\n  ok  $f"; else echo "ERROR"; out="$out\n  ERR $f (${r:-no response})"; fail=1; fi
  done
  if ui_yesno "Reboot" "Uploads:$out\n\nReboot now (apply the update)?"; then
    curl -s --max-time 4 -H "Expect:" -X POST "http://$host/reboot" >/dev/null 2>&1
    ui_msg "Done" "Reboot sent. Device comes back in ~15 s:\n  http://$host/"
  else
    ui_msg "Done" "Uploaded (active after reboot):$out"
  fi
}

# ── 3) Find ──────────────────────────────────────────────────────────────────────
do_scan(){
  clear; echo "### Searching for ESPs ###"; collect_devices
  local d out=""
  [ "${#DEVICES[@]}" = 0 ] && { ui_msg "Search" "No ESPs found."; return; }
  for d in "${DEVICES[@]}"; do out="$out\n  $d   $(info_str "$d")"; done
  ui_msg "Devices found (${#DEVICES[@]})" "$out"
}

# ── Main menu ────────────────────────────────────────────────────────────────────
while true; do
  CH="$(ui_menu "What would you like to do?" "OWON XDM1041 Remote – Flasher" \
    "1" "First flash via USB  (MicroPython + everything)" \
    "2" "OTA update           (network, with selection)" \
    "3" "Find ESPs on the network" \
    "4" "Quit")" || exit 0
  case "${CH:-}" in
    1) usb_flash;;
    2) ota_update;;
    3) do_scan;;
    4|"") exit 0;;
  esac
done
