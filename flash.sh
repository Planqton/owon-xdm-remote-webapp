#!/usr/bin/env bash
#####################################################################################
# OWON XDM1041 Remote – Flash- & OTA-Helfer (whiptail-TUI)
#
#   Menü:
#     1) Erstflash per USB  (MicroPython + alle Firmware-Dateien)
#     2) OTA-Update         (Netzwerk; findet ALLE ESPs inkl. Recovery/AP-Modus,
#                            mit Geräte-Auswahl bei mehreren)
#     3) ESPs im Netzwerk suchen
#
#   Quelle der Firmware: ./firmware/   (neben diesem Skript)
#####################################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW_DIR="$SCRIPT_DIR/firmware"
PORT="${PORT:-/dev/ttyUSB0}"
LAST_HOST_FILE="$SCRIPT_DIR/.owon_last_host"
BT="OWON XDM1041 Remote"

# App-Satz = OTA-sicher (good/-Snapshot, Auto-Rollback)
APP_FILES=(dbg.py ota.py recovery.py wifi_manager.py app.py)
# Bootloader/Trust-Root = nur per USB (nicht rollback-geschützt)
CORE_FILES=(wd.py main.py)
FILES=("${CORE_FILES[@]}" "${APP_FILES[@]}")

MPY_BIN="$SCRIPT_DIR/ESP32_GENERIC-v1.28.0.bin"
MPY_URL="https://micropython.org/resources/firmware/ESP32_GENERIC-20260406-v1.28.0.bin"

HAS_WT=0; command -v whiptail >/dev/null 2>&1 && HAS_WT=1

DEVICES=()

# ── UI-Helfer (whiptail; Text-Fallback falls nicht vorhanden) ─────────────────────
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
    while [ $# -ge 3 ]; do read -rp "$1 ? (j/N) " a >&2; [[ "${a:-}" =~ ^[jJ] ]] && out="$out \"$1\""; shift 3; done
    echo "$out"
  fi
}
ui_msg(){ if [ "$HAS_WT" = 1 ]; then whiptail --backtitle "$BT" --title "$1" --msgbox "$2" 18 76
  else echo; echo "== $1 =="; echo -e "$2"; read -rp "[Enter]" _; fi; }
ui_yesno(){ if [ "$HAS_WT" = 1 ]; then whiptail --backtitle "$BT" --title "$1" --yesno "$2" 14 76
  else read -rp "$2 (j/N) " a; [[ "${a:-}" =~ ^[jJ] ]]; fi; }

mp(){ python3 -m mpremote connect "$PORT" "$@"; }

need_tools(){
  command -v python3 >/dev/null 2>&1 || return 1
  command -v curl    >/dev/null 2>&1 || return 1
  if ! python3 -m mpremote version >/dev/null 2>&1; then
    echo "Installiere mpremote/esptool ..."
    python3 -m pip install --user --break-system-packages mpremote esptool >/dev/null 2>&1 \
      || python3 -m pip install mpremote esptool
  fi
}

# ── ESP-Erkennung (universell: /ota existiert in App, Recovery UND AP-Portal) ─────
is_esp(){ curl -s --max-time "${2:-2}" "http://$1/ota" 2>/dev/null | grep -q "OTA Update"; }

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
    if is_esp "$h" 2; then ip="$(get_ip "$h")"; case "$seen" in *" $ip "*) ;; *) DEVICES+=("$ip"); seen="$seen$ip ";; esac; fi
  done
  base="$(subnet_base)"; echo "Scanne ${base}.1-254 ..."
  tmp="$(mktemp)"
  for n in $(seq 1 254); do ( is_esp "${base}.${n}" 1 && echo "${base}.${n}" >> "$tmp" ) & (( n % 50 == 0 )) && wait; done
  wait
  while read -r ip; do [ -n "$ip" ] || continue; case "$seen" in *" $ip "*) ;; *) DEVICES+=("$ip"); seen="$seen$ip ";; esac; done < <(sort -t. -k4 -n "$tmp" 2>/dev/null)
  rm -f "$tmp"
}

flash_mpy(){
  [ -f "$MPY_BIN" ] || { echo "Lade MicroPython v1.28.0 ..."; curl -fSL -o "$MPY_BIN" "$MPY_URL" || return 1; }
  echo "Lösche Flash ..."; python3 -m esptool --port "$PORT" erase-flash || return 1
  echo "Schreibe MicroPython @0x1000 ..."; python3 -m esptool --port "$PORT" --baud 460800 write-flash 0x1000 "$MPY_BIN" || return 1
}

# ── 1) Erstflash per USB ─────────────────────────────────────────────────────────
usb_flash(){
  need_tools || { ui_msg "Fehler" "python3 oder curl fehlt."; return; }
  if [ ! -e "$PORT" ]; then
    ui_msg "USB" "Kein $PORT gefunden.\n\n- USB-Kabel an den ESP, OWON-5V/V_IN abziehen.\n- Anderer Port:  PORT=/dev/ttyUSB1 ./flash.sh"; return
  fi
  ui_yesno "Erstflash per USB" "MicroPython (falls nötig) flashen und alle Firmware-Dateien auf $PORT spielen?\n\nWICHTIG: OWON-5V/V_IN abgezogen?" || return
  clear; echo "### Erstflash per USB ($PORT) ###"
  local doflash=0
  if mp exec "print(1)" >/dev/null 2>&1; then
    ui_yesno "MicroPython" "MicroPython ist bereits drauf.\n\nNEU flashen?" && doflash=1
  else doflash=1; fi
  if [ "$doflash" = 1 ]; then clear; echo "### MicroPython flashen ###"; flash_mpy || { ui_msg "Fehler" "MicroPython-Flash fehlgeschlagen (siehe Terminal)."; return; }; sleep 2; fi
  clear; echo "### Dateien hochladen ###"; local f okc=0
  for f in "${FILES[@]}"; do
    [ -f "$FW_DIR/$f" ] || { echo "FEHLT: $f"; continue; }
    mp fs cp "$FW_DIR/$f" ":$f" >/dev/null 2>&1 && { echo "  ok $f"; okc=$((okc+1)); } || echo "  FEHLER $f"
  done
  echo "good/-Snapshot anlegen ..."
  mp exec "import os
try:
    os.mkdir('good')
except Exception:
    pass" >/dev/null 2>&1
  for f in "${APP_FILES[@]}"; do mp fs cp "$FW_DIR/$f" ":good/$f" >/dev/null 2>&1; done
  mp exec "open('boot.cnt','w').write('0')" >/dev/null 2>&1
  mp exec "import machine;machine.reset()" >/dev/null 2>&1 || true
  ui_msg "Fertig" "$okc/${#FILES[@]} Dateien geflasht + good/-Snapshot, Reset.\n\nNächster Schritt:\n  WLAN 'OWON-XDM-Remote-Setup' -> http://192.168.4.1\n  -> dein WLAN einrichten -> http://owon.local/"
}

# ── 2) OTA-Update ────────────────────────────────────────────────────────────────
ota_update(){
  need_tools || { ui_msg "Fehler" "python3 oder curl fehlt."; return; }
  clear; echo "### Suche ESPs im Netzwerk ###"
  collect_devices
  local n="${#DEVICES[@]}" host d
  if [ "$n" = 0 ]; then ui_msg "OTA" "Kein ESP gefunden.\n\n- eingeschaltet & im selben Netz?\n- im Setup-Modus: erst mit WLAN 'OWON-XDM-Remote-Setup' verbinden, dann erneut."; return; fi
  if [ "$n" = 1 ]; then host="${DEVICES[0]}"; ui_yesno "1 Gerät gefunden" "$host\n[$(info_str "$host")]\n\nDieses Gerät verwenden?" || return
  else
    local args=(); for d in "${DEVICES[@]}"; do args+=("$d" "$(info_str "$d")"); done
    host="$(ui_menu "Gerät auswählen ($n gefunden)" "Welcher ESP soll geflasht werden?" "${args[@]}")" || return
    [ -z "$host" ] && return
  fi
  echo "$host" > "$LAST_HOST_FILE"
  local choice sel=()
  choice="$(ui_menu "Dateien" "Was auf $host hochladen?" \
    "all"  "Kompletter App-Satz (empfohlen, OTA-sicher)" \
    "app"  "Nur app.py" \
    "pick" "Einzeln auswählen (App-Satz)" \
    "core" "Bootloader main.py/wd.py (VORSICHT!)")" || return
  case "$choice" in
    all) sel=("${APP_FILES[@]}");;
    app) sel=(app.py);;
    pick) local cargs=(); for d in "${APP_FILES[@]}"; do cargs+=("$d" "" off); done
          local res; res="$(ui_check "Dateien wählen" "Leertaste = an/aus, Enter = OK" "${cargs[@]}")" || return
          eval "sel=($res)";;
    core) ui_yesno "VORSICHT – Bootloader" "main.py/wd.py sind der Trust-Root (Bootloader).\nEin kaputtes Update hier kann den ESP BRICKEN (nur per USB rettbar).\n\nWirklich per OTA flashen?" || return; sel=("${CORE_FILES[@]}");;
    *) return;;
  esac
  [ "${#sel[@]}" = 0 ] && { ui_msg "OTA" "Nichts ausgewählt."; return; }
  clear; echo "### OTA-Upload -> $host ###"; local f r out="" fail=0
  for f in "${sel[@]}"; do
    [ -f "$FW_DIR/$f" ] || { echo "FEHLT: $f"; out="$out\n  FEHLT $f"; fail=1; continue; }
    printf "Upload %-18s " "$f ..."
    r="$(curl -s --max-time 90 -H "Expect:" -X POST --data-binary @"$FW_DIR/$f" "http://$host/upload?name=$f" 2>/dev/null)"
    if echo "$r" | grep -q '"ok":true'; then echo "ok"; out="$out\n  ok  $f"; else echo "FEHLER"; out="$out\n  ERR $f (${r:-keine Antwort})"; fail=1; fi
  done
  if ui_yesno "Neustart" "Uploads:$out\n\nJetzt neu starten (Update aktivieren)?"; then
    curl -s --max-time 4 -H "Expect:" -X POST "http://$host/reboot" >/dev/null 2>&1
    ui_msg "Fertig" "Reboot gesendet. Gerät kommt in ~15 s zurück:\n  http://$host/"
  else
    ui_msg "Fertig" "Hochgeladen (erst nach Neustart aktiv):$out"
  fi
}

# ── 3) Suchen ────────────────────────────────────────────────────────────────────
do_scan(){
  clear; echo "### Suche ESPs ###"; collect_devices
  local d out=""
  [ "${#DEVICES[@]}" = 0 ] && { ui_msg "Suche" "Keine ESPs gefunden."; return; }
  for d in "${DEVICES[@]}"; do out="$out\n  $d   $(info_str "$d")"; done
  ui_msg "Gefundene Geräte (${#DEVICES[@]})" "$out"
}

# ── Hauptmenü ────────────────────────────────────────────────────────────────────
while true; do
  CH="$(ui_menu "Was möchtest du tun?" "OWON XDM1041 Remote – Flasher" \
    "1" "Erstflash per USB  (MicroPython + alles)" \
    "2" "OTA-Update         (Netzwerk, mit Auswahl)" \
    "3" "ESPs im Netzwerk suchen" \
    "4" "Beenden")" || exit 0
  case "${CH:-}" in
    1) usb_flash;;
    2) ota_update;;
    3) do_scan;;
    4|"") exit 0;;
  esac
done
