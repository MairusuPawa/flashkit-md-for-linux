# FlashKit MD CLI

A Python command-line tool for reading and writing Mega Drive / 32X cartridges with a FlashKit MD serial programmer.

## Requirements

- Python 3.10 or newer
- pySerial 3.5 or newer (`python-pyserial` on Arch Linux)
- Read/write access to the FlashKit serial device

The FlashKit serial protocol backend is included in this repository; no separate `flashkit` Python package is needed. To create a development environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```sh
.venv/bin/python flashkit-md.py --check
.venv/bin/python flashkit-md.py --layout
.venv/bin/python flashkit-md.py --read dump.bin
.venv/bin/python flashkit-md.py --write game.32x
.venv/bin/python flashkit-md.py --device /dev/ttyUSB0 --write game.bin
```

When exactly one FT232 device (USB ID `0403:6001`) is present, the tool detects its serial port automatically. ROM writes require confirmation unless `--yes` is specified. The write worker exits after FlashKit's own verification, closing its serial handle; the parent reconnects and compares a second readback against the exact prepared image.

## Hardware and image limits

The default software write limit is 4 MiB. `--max-size BYTES` can raise or disable that limit, but this software cannot identify the physical capacity of the attached chip or prove that a clone's flash command sequence is compatible. CFI detection alone does not establish write compatibility. Larger images require suitable flash capacity and mapper support.

The reconnect step closes and reopens the serial session; it does not electrically reset or power-cycle the programmer. The reference FlashKit C# source clamps writes at 4 MiB. AliExpress clone hardware may differ from the reference design.

## License and attribution

The protocol backend is derived from the [GPL-3.0-or-later FlashKit MD Python Client](https://github.com/joeyparrish/flashkit-md-py) by Joey Parrish, which in turn derives from [Krikzz's FlashKit MD software](https://github.com/krikzz/flashkit). See [LICENSE](LICENSE) and the notices in `flashkit_md_backend.py`.

## Arch packaging notes

Install the executable as `/usr/bin/flashkit-md`, `flashkit_md_backend.py` in Python site-packages, and the manual as `/usr/share/man/man1/flashkit-md.1`. Runtime dependencies are Python and `python-pyserial`. No AUR metadata is included in this repository yet.
