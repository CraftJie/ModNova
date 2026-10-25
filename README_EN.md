# ModNova

A Modbus debugging tool with **master and slave in one app**: a single-window desktop program
speaking **Modbus TCP**, **RTU** (serial) and **RTU over TCP** (serial device servers, DTUs, 4G
gateways).

Point it at a device and poll registers continuously, interpret the raw 16-bit words as the type
they really are (`float32`, counters, ASCII), rewrite a single register in place, paste any frame
as hex and see it broken down field by field — or start a slave yourself and work without hardware.

## Download & install

Windows 10 / 11, 64-bit. **Portable: unzip and run — no Python install, serial support included.**

1. Open [Releases](https://github.com/CraftJie/ModNova/releases/latest) and download
   `ModNova-<version>-win-x64.zip` (about 12 MB)
2. Unzip anywhere, e.g. `D:\Tools\ModNova`
3. Double-click `ModNova\ModNova.exe`

If Windows shows "Windows protected your PC", click **More info → Run anyway** — the build uses a
self-signed certificate, which is common for open-source projects. To verify the download, compare
the zip's SHA256 against `SHA256SUMS.txt` on the same release page.

## Quick start

Pick TCP or serial in the link panel, enter the host and unit ID, hit "connect" — polling starts
right away, no second button.

## Usage

**Reading.** Choose function code 01/02/03/04, a start address and a count; the table refreshes on
every poll (there is no separate "read" button — new parameters apply on the next poll). Press `F5`
for an immediate single read. The dropdown next to the address field decides how that text is
interpreted: decimal, hexadecimal, or PLC addressing (starting at 40001, what device manuals
usually list). The rightmost toolbar button switches between **overview** (a Modbus Poll style
grid) and **detail** (one address per row, decimal / hex / binary side by side).

**Interpreting values.** Set type, byte order, scale factor, offset and unit in "data definition":

- Types: `uint16 / int16 / uint32 / int32 / float32 / uint64 / int64 / float64 / ascii`
- Byte orders: `ABCD / BADC / CDAB / DCBA`
- Scaling: `displayed = raw × factor + offset` (inverted automatically on write)

A `float32` that reads as an astronomical number, or a negative value that looks wrong, almost
always comes down to these two: try all four byte orders, and switch to `int16 / int32` when the
value can be negative.

**Writing.** Pick a write function code (05/06/0F/10/16), enter the value, press `F5` — the tool
reads the register back afterwards to confirm it actually landed. You can also double-click any
cell to edit it in place: `Enter` commits, `Esc` cancels.

**Running a slave.** The slave tab starts a TCP or serial slave in the same window, so the master
can connect to it and you can test without any real hardware. It listens on `127.0.0.1:5020` by
default (port 502 is often taken by other Modbus software). Modbus has no authentication — unless
you are on an isolated lab network, do not change the listen address to `0.0.0.0`.

**Watching frames.** Every raw frame in and out is in the message log, and the slave tab can
misbehave on purpose (dropped frames, delays, exception codes, bad CRC) to exercise your master's
error handling.

**Shortcuts.** `F5` execute once · `Esc` stop polling · `Ctrl+1 / Ctrl+2` master / slave ·
`Ctrl+I` read device identification · right-click copies the table as TSV for Excel.

## Troubleshooting

- **SmartScreen blocks it** — click "More info → Run anyway". The build has no commercial code
  signing certificate.
- **Port 502 won't bind** — another Modbus program owns it, or you lack privileges. The simulator
  defaults to 5020; point the client there.
- **Serial port won't open** — usually another program holding it; close that program and retry.
  For USB adapters, confirm the port number in Device Manager first.
- **Values don't match the manual** — check the address notation first (a manual's 40001 is usually
  protocol address 0), then the byte order; `float32` is the usual culprit.
- **Header shows "no response"** — look at TX/RX in the log: TX only means unit ID, wiring or
  serial settings; both TX and RX but CRC errors means baud rate or interference.
