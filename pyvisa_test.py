#!/usr/bin/env python3
"""
PyVISA test for the OWON XDM1041 Remote (SCPI-TCP server on port 5025).

Usage:
    ./pyvisa_test.py                 # default host owon.local
    ./pyvisa_test.py owon.local      # another hostname
    ./pyvisa_test.py 192.168.0.85    # direct IP
    ./pyvisa_test.py owon.local --switch   # also run a write demo (switch function)

Install (PC):
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
    print("PyVISA is not installed. Install with:")
    print("  python3 -m pip install --user --break-system-packages pyvisa pyvisa-py")
    sys.exit(1)

resource = 'TCPIP::{}::{}::SOCKET'.format(HOST, PORT)
print("Connecting to {} ...".format(resource))

rm = pyvisa.ResourceManager('@py')   # '@py' = pure pyvisa-py backend (no NI-VISA needed)
try:
    inst = rm.open_resource(resource)
except Exception as e:
    print("ERROR: connection failed:", e)
    print("  - ESP powered on and on the same network?")
    print("  - hostname/IP correct? e.g.:  ./pyvisa_test.py 192.168.0.85")
    sys.exit(1)

inst.read_termination = '\n'
inst.write_termination = '\n'
inst.timeout = 4000   # ms


def q(cmd):
    try:
        r = inst.query(cmd).strip()
    except Exception as e:
        r = "(error: {})".format(e)
    print("  {:<16} -> {}".format(cmd, r))


print("\n== Device info & status (read-only) ==")
q('*IDN?')
q('FUNC?')
q('RANGE?')
q('RATE?')

print("\n== 5 readings ==")
for _ in range(5):
    q('MEAS?')
    time.sleep(0.3)

if DO_SWITCH:
    print("\n== Write demo: switch to AC, measure, back to DC ==")
    inst.write('CONF:VOLT:AC')
    time.sleep(0.9)
    q('FUNC?')
    q('MEAS?')
    inst.write('CONF:VOLT:DC')
    time.sleep(0.6)
    q('FUNC?')
    print("  (back on DC)")
else:
    print("\n(tip: pass  --switch  to also run a write demo)")

inst.close()
print("\nDone. ok")
