#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read, write, and inspect Mega Drive / 32X carts with a FlashKit MD."""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import serial
import serial.tools.list_ports
from flashkit_md_backend import (
    ROM_SIZE_MULTIPLE,
    cart_layout,
    check_cart,
    read_rom,
    write_rom,
)

FTDI_VID = 0x0403
FTDI_PID = 0x6001
DEFAULT_MAX_ROM_SIZE = 4 * 1024 * 1024
RECONNECT_TIMEOUT = 20.0
RECONNECT_INTERVAL = 0.5
LOG = logging.getLogger("flashkit-md")


def find_device(required: bool = True) -> str | None:
    matches = [
        port.device
        for port in serial.tools.list_ports.comports()
        if port.vid == FTDI_VID and port.pid == FTDI_PID
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and not required:
        return None
    found = ", ".join(matches) if matches else "none"
    raise RuntimeError(
        f"Expected one FT232 device (0403:6001), found {len(matches)}: {found}. "
        "Select a serial port with --device."
    )


def probe(port: str, show_layout: bool = False) -> None:
    if show_layout:
        layout = cart_layout(port)
        print(f"Cartridge: {layout['name']}")
        print(f"Header-declared ROM size: {layout['header_size']:,} bytes")
        print(f"Detected addressable ROM size: {layout['addressable_size']:,} bytes")
        print(f"Detected RAM size: {layout['ram_size']:,} bytes")
        print("Flash chip capacity is not reported by this client.")
    else:
        name = check_cart(port)
        print(f"FlashKit and cartridge respond (CFI signature valid): {name}")


def confirm(prompt: str, skip: bool) -> bool:
    if skip:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def read_cartridge(port: str, output: Path, skip_confirmation: bool) -> bool:
    if output.exists() and not confirm(f"Overwrite {output}?", skip_confirmation):
        print("Cancelled.")
        return False

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        size, digest = read_rom(port, temp_path)
        os.replace(temp_path, output)
    finally:
        temp_path.unlink(missing_ok=True)

    print(f"Read {size:,} bytes to {output}; MD5: {digest}")
    return True


def verify_after_reconnect(port: str | None, expected_path: Path) -> None:
    expected = expected_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="flashkit-md-") as temp_dir:
        readback_path = Path(temp_dir) / "readback.bin"
        deadline = time.monotonic() + RECONNECT_TIMEOUT
        last_error: str | None = None

        while time.monotonic() < deadline:
            candidate = port or find_device(required=False)
            if candidate:
                try:
                    LOG.info("Reconnecting on %s for independent readback", candidate)
                    read_rom(candidate, readback_path, len(expected))
                except (OSError, RuntimeError) as exc:
                    last_error = str(exc)
                    LOG.debug("Reconnect/readback attempt failed: %s", exc)
                else:
                    actual = readback_path.read_bytes()
                    if actual != expected:
                        limit = min(len(actual), len(expected))
                        offset = next(
                            (i for i in range(limit) if actual[i] != expected[i]),
                            limit,
                        )
                        raise RuntimeError(
                            f"Reconnect readback mismatch at byte {offset} "
                            f"(expected {len(expected)} bytes, read {len(actual)})"
                        )
                    print("Reconnect readback verification succeeded.")
                    return
            else:
                LOG.debug("FlashKit USB serial device is not present yet")
            time.sleep(RECONNECT_INTERVAL)

    raise RuntimeError(
        f"Could not reconnect and verify the cart within {RECONNECT_TIMEOUT:g}s"
        + (f": {last_error}" if last_error else "")
    )


def run_write_worker(
    port: str, rom_path: Path, prepared_output: Path, max_size: int
) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_write-worker",
        str(rom_path),
        "--device",
        port,
        "--_prepared-output",
        str(prepared_output),
        "--_max-size",
        str(max_size),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RuntimeError(f"FlashKit writer exited with status {result.returncode}")


def write_cartridge(
    port: str,
    rom_path: Path,
    max_size: int,
    skip_confirmation: bool,
    reconnect_port: str | None = None,
) -> bool:
    if not rom_path.is_file():
        raise ValueError(f"ROM file not found: {rom_path}")
    rom_path = rom_path.resolve()
    size = rom_path.stat().st_size
    if size == 0:
        raise ValueError("ROM file is empty")
    if max_size < 0:
        raise ValueError("--max-size must be zero or a positive number of bytes")
    padded_size = ((size + ROM_SIZE_MULTIPLE - 1) // ROM_SIZE_MULTIPLE) * ROM_SIZE_MULTIPLE
    if max_size and padded_size > max_size:
        raise ValueError(
            f"ROM pads to {padded_size} bytes; configured software limit is {max_size} bytes"
        )

    print(f"Device: {port}")
    print(f"ROM: {rom_path} ({size:,} bytes; padded to {padded_size:,} bytes)")
    if padded_size > DEFAULT_MAX_ROM_SIZE:
        print("WARNING: ROM exceeds 4 MiB; the reference GUI clamps writes at 4 MiB, "
              "and this tool cannot identify the clone cart's physical capacity or mapper.")
    probe(port)
    if not confirm("Erase and flash this cartridge?", skip_confirmation):
        print("Cancelled.")
        return False

    with tempfile.TemporaryDirectory(prefix="flashkit-md-write-") as temp_dir:
        prepared_path = Path(temp_dir) / "prepared.bin"
        run_write_worker(port, rom_path, prepared_path, max_size)
        verify_after_reconnect(reconnect_port, prepared_path)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("-r", "--read", metavar="FILE", type=Path,
                       help="read the cartridge ROM to FILE")
    modes.add_argument("-w", "--write", metavar="FILE", type=Path,
                       help="write FILE to the cartridge and verify it")
    modes.add_argument("-c", "--check", action="store_true",
                       help="check FlashKit and cartridge communication")
    modes.add_argument("-l", "--layout", action="store_true",
                       help="report detected ROM and RAM measurements")
    parser.add_argument("rom", nargs="?", type=Path,
                        help="ROM image to write (shorthand for --write FILE)")
    parser.add_argument("-d", "--device", "--port", dest="device",
                        help="serial port (auto-detected by USB ID by default)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show reconnect and device diagnostics")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="skip write confirmation and allow output overwrite")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_ROM_SIZE,
                        metavar="BYTES",
                        help="software write limit (default: 4194304; 0 disables it)")
    parser.add_argument("--_write-worker", metavar="FILE", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument("--_prepared-output", metavar="FILE", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument("--_max-size", type=int, default=DEFAULT_MAX_ROM_SIZE,
                        help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args._write_worker:
        if not args.device or not args._prepared_output:
            parser.error("internal writer requires --device and --_prepared-output")
        try:
            size, digest = write_rom(
                args.device, args._write_worker, args._prepared_output, args._max_size
            )
            print(f"FlashKit same-session verification succeeded ({size:,} bytes, MD5 {digest}).")
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.rom and (args.read or args.write or args.check or args.layout):
        parser.error("positional ROM cannot be combined with -r, -w, -c, or -l")
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    try:
        port = args.device or find_device()
        if args.read:
            return 0 if read_cartridge(port, args.read, args.yes) else 1
        if args.write or args.rom:
            path = args.write or args.rom
            return 0 if write_cartridge(
                port, path, args.max_size, args.yes, args.device
            ) else 1
        probe(port, show_layout=args.layout)
        return 0
    except (EOFError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
