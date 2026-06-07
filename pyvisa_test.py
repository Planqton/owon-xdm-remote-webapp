#!/usr/bin/env python3
"""
PyVISA-Test für den OWON XDM1041 Remote (SCPI-TCP-Server auf Port 5025).

Aufruf:
    ./pyvisa_test.py                 # Standard-Host owon.local
    ./pyvisa_test.py owon.local      # anderer Hostname
    ./pyvisa_test.py 192.168.0.85    # direkte IP
    ./pyvisa_test.py owon.local --switch   # zusätzlich Schreib-Demo (Funktion umschalten)

Installation (PC):
    python3 -m pip install --user --break-system-packages pyvisa pyvisa-py
"""
import sys
import time

HOST = next((a for a in sys.argv[1:] if not a.startswith('-')), 'owon.local')
PORT = 5025
DO_SWITCH = '--switch' in sys.argv

try:
    import pyvisa
except ImportError:
    print("PyVISA ist nicht installiert. Installieren mit:")
    print("  python3 -m pip install --user --break-system-packages pyvisa pyvisa-py")
    sys.exit(1)

resource = 'TCPIP::{}::{}::SOCKET'.format(HOST, PORT)
print("Verbinde mit {} ...".format(resource))

rm = pyvisa.ResourceManager('@py')   # '@py' = reines pyvisa-py-Backend (kein NI-VISA noetig)
try:
    inst = rm.open_resource(resource)
except Exception as e:
    print("FEHLER: Verbindung fehlgeschlagen:", e)
    print("  - ESP eingeschaltet und im selben Netz?")
    print("  - Hostname/IP korrekt? Aufruf z. B.:  ./pyvisa_test.py 192.168.0.85")
    sys.exit(1)

inst.read_termination = '\n'
inst.write_termination = '\n'
inst.timeout = 4000   # ms


def q(cmd):
    try:
        r = inst.query(cmd).strip()
    except Exception as e:
        r = "(Fehler: {})".format(e)
    print("  {:<16} -> {}".format(cmd, r))


print("\n== Geraete-Info & Status (read-only) ==")
q('*IDN?')
q('FUNC?')
q('RANGE?')
q('RATE?')

print("\n== 5 Messungen ==")
for _ in range(5):
    q('MEAS?')
    time.sleep(0.3)

if DO_SWITCH:
    print("\n== Schreib-Demo: auf AC umschalten, messen, zurueck auf DC ==")
    inst.write('CONF:VOLT:AC')
    time.sleep(0.9)
    q('FUNC?')
    q('MEAS?')
    inst.write('CONF:VOLT:DC')
    time.sleep(0.6)
    q('FUNC?')
    print("  (wieder auf DC gestellt)")
else:
    print("\n(Tipp: mit  --switch  wird zusaetzlich eine Schreib-Demo ausgefuehrt)")

inst.close()
print("\nFertig. ✔")
