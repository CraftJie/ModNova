# ModNova

A lightweight Modbus debugging tool with **master & slave in one app**, speaking **Modbus TCP and Modbus RTU** — plus **RTU over TCP** for serial-device servers, DTUs and 4G gateways.

The GUI is a single-window Tkinter desktop app; the protocol core is pure Python standard library — only the RTU serial mode needs `pyserial`.

What makes it a daily driver: every register can be interpreted as **9 data types** (including ASCII strings) × **4 byte orders**, with scaling, engineering units and PLC-style addressing — and every raw frame on the wire is visible in the message log.

![master view](images/master.png)

## Capability matrix

| FC | Name | Master | Slave |
|---|---|---|---|
| 01 | Read Coils | ✅ | ✅ |
| 02 | Read Discrete Inputs | ✅ | ✅ |
| 03 | Read Holding Registers | ✅ | ✅ |
| 04 | Read Input Registers | ✅ | ✅ |
| 05 | Write Single Coil | ✅ | ✅ |
| 06 | Write Single Register | ✅ | ✅ |
| 0F | Write Multiple Coils | ✅ | ✅ |
| 10 | Write Multiple Registers | ✅ | ✅ |
| 16 | Mask Write Register | ✅ | ✅ |
| 17 | Read/Write Multiple Registers | core | ✅ |
| 2B | Read Device Identification | core (TCP/RTU) | ✅ |

Exception codes are generated and parsed per spec: a master-side exception is reported as an exception (never silently as "read zero"), and the UI translates codes into plain language.

## Data types & addressing

| Type | Size | Notes |
|---|---|---|
| `uint16` / `int16` | 1 register | default |
| `uint32` / `int32` | 2 registers | counters, energy meters |
| `float32` | 2 registers | temperature, power, flow — the classic pitfall |
| `uint64` / `int64` / `float64` | 4 registers | rare but real |
| `ascii` | 1 register = 2 chars | model names, serial numbers |

Four byte orders (`ABCD` / `BADC` / `CDAB` / `DCBA`) cover both word order across registers and byte order within a register. The same `220.5` maps to four completely different register sets — if your manual says `float32` but you read astronomical numbers, this is why.

- **Scaling**: `display = raw × scale (+ bias)`; writes are inverted automatically.
- **Addressing**: protocol addresses start at 0, PLC addresses at 40001. Enable "PLC addressing" and type 40001 directly; mismatched areas are rejected with a clear error.

## UI

The master tab is one toolbar, one table, one status bar. Connection parameters collapse once connected; the data-interpretation panel (type/byte order/scale/unit + addressing) lives in a dialog with a live echo in the status bar; the message log folds into a floating overlay that never shrinks the table.

Highlights:

- **Write with verification** — after every write the tool automatically reads back with the matching read FC and reports how many registers actually landed (float32 = 2, float64 = 4), highlighting the affected cells for 2.6 s. You see "written *and confirmed*", not just "sent".
- **In-place editing** — double-click any cell to write that address; coils toggle; read-only areas refuse politely.
- **Link health** — the header shows `● online · TCP 127.0.0.1:502 · unit 1`; consecutive failures degrade to `no response ×N` then `link down`, and recover automatically.
- **Slave simulator** — start a TCP / RTU / RTU-over-TCP slave in the same window, including **fault injection** (drop, delay, exceptions, truncation, bad CRC) to test your own master's error handling without any real hardware.

![slave view](images/slave.png)

### Keyboard shortcuts

| Key | Action |
|---|---|
| `F5` / `Ctrl+Enter` | execute once (read or write, per current FC) |
| `Ctrl+1` / `Ctrl+2` / `F6` | master tab / slave tab / toggle |
| `Esc` | stop polling |
| `Ctrl+L` | clear log |
| right-click | copy cell / copy table (TSV, paste into Excel) |
| double-click value | edit & write in place (Enter commits, Esc cancels) |

## Running

Python 3.9+ required; RTU serial mode additionally needs `pip install pyserial` (or `pip install -e ".[rtu]"`).

```bash
python modnova.py                       # GUI
python modnova.py read --tcp 192.168.1.10 --fc 3 --addr 0 --count 4 \
    --type float32 --byteorder CDAB --unit-label °C
python modnova.py read --tcp 192.168.1.10 --fc 3 --addr 40001 --count 4 --plc
python modnova.py read --rtutcp 192.168.1.10:502 --fc 3 --addr 0 --count 4
python modnova.py serve --tcp 0.0.0.0:5020 --unit 1 --init 100,200,300
python modnova.py serve --rtutcp 0.0.0.0:5020 --unit 1   # bare RTU frames over TCP

pip install -e ".[rtu]"                 # installs the `modnova` entry point
modnova ports
```

## Design decisions worth knowing

- **TCP framing by MBAP length field** — read the 7-byte header, then exactly `length-1` bytes; several responses in one `recv` can never misalign.
- **RTU framing by function code, not 3.5-char silence** — the silent interval is unreliable on PCs (USB-serial jitter); knowing unit + FC determines the whole frame length.
- **Retries only for timeouts / link errors** — an `ILLEGAL_DATA_ADDRESS` from the slave is an *answer*, not a fault. Writes are **never retried**: resending might write twice.
- **Slave returns exceptions on out-of-range access, never zeros** — silently padded zeros hide address-calculation bugs during commissioning.

## Architecture

```
    gui.py (Tk)      cli.py      core.py (headless API)
        │              │              │
        └──────────────┴──────┬───────┘
                              │
                  ┌───────────┴───────────┐
                  │  client.py  server.py │
                  └───────────┬───────────┘
                  ┌───────────┴───────────┐
                  │        pdu.py         │  FC ↔ bytes, no addressing/CRC/MBAP
                  └──────────┬────────────┘
            ┌────────────────┴────────────────┐
       ┌────┴─────┐                     ┌─────┴─────┐
       │ MBAP+TCP │                     │ addr+CRC  │  (serial or socket)
       └──────────┘                     └───────────┘
```

The PDU layer carries no unit address, CRC or MBAP header — that is all transport packaging. You can feed bytes straight into `ModbusServer.handle_pdu()` in unit tests, or use the package as a library:

```python
from modbus_tool import ModbusCore
app = ModbusCore()
app.connect({"mode": "tcp", "host": "192.168.1.10", "port": 502, "unit": 1})
print(app.read(3, 0, 4, dtype="float32", byteorder="CDAB")["rows"])
```

## Tests

```bash
python tests/run_all.py     # logic + UI + RTU suites in one go

python -m unittest discover -s tests -v        # logic suite (200+ tests, no Tk needed)
python tests/ui_full_test.py                   # UI suite (needs a Tk-interpreter)
python tests/rtu_full_test.py                  # RTU suite (pyserial or bundled stub)
```

## Packaging

```bash
python -m venv build/.venv
build/.venv/Scripts/pip install -r build/requirements-build.txt
build/.venv/Scripts/python build/build.py     # produces build/dist/ModNova/ + zip
```

`build/sign.ps1` applies an Authenticode signature with an RFC 3161 timestamp. For public releases, prefer a proper signing identity (e.g. SignPath Foundation for open-source projects) over a self-signed certificate.

## FAQ

**Port 502 is taken / refused.** Another Modbus program often owns it; Linux needs root for <1024. The slave defaults to 5020 — point the client at it.

**The slave only listens on 127.0.0.1 by default.** Modbus has no authentication; binding `0.0.0.0` exposes a writable memory block to your whole LAN segment. Change it explicitly when needed.

**Values don't match the device manual.** Check the protocol-vs-PLC address conversion first, then the byte order — `float32` is the usual suspect.

**The header shows "no response".** Look at the log: TX only → wiring/unit/serial settings; TX+RX but CRC errors → baud rate or interference.

## License

MIT
