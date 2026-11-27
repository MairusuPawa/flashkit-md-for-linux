# SPDX-License-Identifier: GPL-3.0-or-later
# FlashKit MD serial protocol backend.
#
# Portions are derived from the FlashKit MD Python Client:
# Copyright (C) 2024 Joey Parrish, https://github.com/joeyparrish/flashkit-md-py
# That client is derived from the FlashKit MD software by Krikzz:
# https://github.com/krikzz/flashkit
# See LICENSE for the full GPL-3.0-or-later license text.

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path

import serial

FTDI_VID = 0x0403
FTDI_PID = 0x6001
BAUD_RATE = 9600
IO_BLOCK_SIZE = 64 * 1024
READ_BLOCK_SIZE = 32 * 1024
WRITE_BLOCK_SIZE = 4 * 1024
ROM_SIZE_MULTIPLE = 64 * 1024
MAX_ROM_SIZE = 32 * 1024 * 1024
CFI_SIGNATURE = b"\x00Q\x00R\x00Y"
TIME_REGISTER = 0xA13000
SRAM_BASE = 0x200000
CART_METADATA_SIGNATURE = b"OPEN-EVERDRIVE"
CART_METADATA_SIGNATURE_OFFSET = 0x120
CART_METADATA_BANK_REGISTER = 0xA130E0
CART_METADATA_BANK_VALUE = 0x0404
CART_METADATA_DATETIME_OFFSET = 0x1C8


class FlashKitError(RuntimeError):
    """Base error for FlashKit communication and cartridge operations."""


class FlashKitNotFoundError(FlashKitError):
    """The selected serial port did not respond as a FlashKit programmer."""


class FlashChipNotFoundError(FlashKitError):
    """The cartridge did not return the expected CFI signature."""


class FlashKitDevice:
    """FlashKit command protocol over its USB serial adapter."""

    def __init__(self, port: str):
        self.port = port
        self.serial = serial.Serial(
            port=port,
            baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=0.2,
        )
        try:
            self._write(b"\x62")  # Read programmer ID.
            device_id = int.from_bytes(self._read_exact(2), "big")
            if device_id == 0 or (device_id & 0xFF) != (device_id >> 8):
                raise FlashKitNotFoundError(f"FlashKit not detected on {port}")
            self.serial.timeout = 2
            self.serial.write_timeout = 2
            self.set_delay(0)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> FlashKitDevice:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self.serial.is_open:
            self.serial.close()

    def _write(self, data: bytes) -> None:
        count = self.serial.write(data)
        if count != len(data):
            raise FlashKitError(f"Short serial write: {count} of {len(data)} bytes")

    def _read_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = self.serial.read(count - len(data))
            if not chunk:
                raise FlashKitError(
                    f"Short serial read: received {len(data)} of {count} bytes"
                )
            data.extend(chunk)
        return bytes(data)

    def set_delay(self, delay: int) -> None:
        self._write(bytes((5, delay & 0xFF)))

    def set_address(self, address: int) -> None:
        if address < 0:
            raise ValueError("address must be non-negative")
        word_address = address >> 1
        self._write(bytes((
            0, (word_address >> 16) & 0xFF,
            0, (word_address >> 8) & 0xFF,
            0, word_address & 0xFF,
        )))

    def read(self, length: int) -> bytes:
        if length < 0 or length % 2:
            raise ValueError("FlashKit reads require a non-negative even byte count")
        output = bytearray()
        while length:
            chunk_length = min(length, IO_BLOCK_SIZE)
            words = chunk_length // 2
            self._write(bytes((1, (words >> 8) & 0xFF, 1, words & 0xFF, 0x82)))
            output.extend(self._read_exact(chunk_length))
            length -= chunk_length
        return bytes(output)

    def write(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("FlashKit writes require an even byte count")
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + IO_BLOCK_SIZE]
            words = len(chunk) // 2
            self._write(bytes((1, (words >> 8) & 0xFF, 1, words & 0xFF, 0x83)))
            self._write(chunk)
            offset += len(chunk)

    def read_u16(self, address: int) -> int:
        self.set_address(address)
        self._write(b"\x42")
        return int.from_bytes(self._read_exact(2), "big")

    def write_u16(self, address: int, value: int) -> None:
        self.set_address(address)
        self._write(b"\x43" + (value & 0xFFFF).to_bytes(2, "big"))

    def write_u8(self, address: int, value: int) -> None:
        self.set_address(address)
        self._write(bytes((0x53, value & 0xFF)))

    def flash_reset(self) -> None:
        self.write_u16(0, 0xF0)

    def check_for_flash(self) -> None:
        self.write_u8(0x55 * 2, 0x98)
        self.set_address(0x20)
        try:
            signature = self.read(len(CFI_SIGNATURE))
        finally:
            self.flash_reset()
        if signature != CFI_SIGNATURE:
            raise FlashChipNotFoundError("CFI query failed; flash chip not detected")

    def flash_reset_bypass(self) -> None:
        self.flash_reset()
        self.write_u8(0, 0x90)
        self.write_u8(0, 0x00)

    def flash_unlock_bypass(self) -> None:
        self.write_u8(0x555 * 2, 0xAA)
        self.write_u8(0x2AA * 2, 0x55)
        self.write_u8(0x555 * 2, 0x20)

    def flash_erase(self, address: int) -> None:
        self.write_u16(0x555 * 2, 0xAA)
        self.write_u16(0x2AA * 2, 0x55)
        self.write_u16(0x555 * 2, 0x80)
        self.write_u16(0x555 * 2, 0xAA)
        self.write_u16(0x2AA * 2, 0x55)
        for _ in range(8):
            self.set_address(address)
            self._write(b"\x53\x30")
            address += 8192
        self._write(b"\x04\x42")
        self._read_exact(2)

    def flash_write(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("Flash programming requires an even byte count")
        command = bytearray()
        for offset in range(0, len(data), 2):
            command.extend((0x53, 0xA0, 0xC3))
            command.extend(data[offset:offset + 2])
            command.append(0x04)
        self._write(bytes(command))


class Cartridge:
    """Mega Drive cartridge header and layout queries."""

    def __init__(self, device: FlashKitDevice):
        self.device = device

    def rom_name(self) -> str:
        self.device.set_address(0)
        header = self.device.read(512)
        valid = b" !()_-:/.[]|&'`0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        name = "Unknown"
        for offset in (0x120, 0x150):
            raw = header[offset:offset + 48].strip(b" \x00\xFF")
            if raw and all(byte in valid for byte in raw):
                name = raw.decode("ascii")
                break
        return f"{name} ({self._region(header)})"

    @staticmethod
    def _region(header: bytes) -> str:
        old = header[0x1F0:0x1F3].strip(b"\x00 ")
        if old and all(byte in b"JUE" for byte in old):
            return old.decode("ascii")
        try:
            flags = int(chr(header[0x1F0]), 16)
        except ValueError:
            return "X"
        regions = "".join((
            "J" if flags & 1 else "",
            "U" if flags & 4 else "",
            "E" if flags & 8 else "",
        ))
        return regions or "X"

    def rom_size(self, trust_header: bool) -> int:
        self.device.set_address(0)
        header = self.device.read(512)
        if trust_header:
            size = int.from_bytes(header[0x1A4:0x1A8], "big") + 1
            if size < 512 or size > 0x02000000:
                raise FlashKitError(f"Invalid ROM size in cartridge header: {size} bytes")
            return size

        has_ram = self.ram_available()
        has_extra_rom = self._detect_extra_rom() if has_ram else False
        max_size = 0x400000 if not has_ram or has_extra_rom else 0x200000
        length = self._check_rom_size(0, max_size)
        if length == 0x400000:
            length = 0x200000
            length2 = self._check_rom_size(0x200000, 0x200000)
            if length2 == 0x200000:
                length2 = self._check_rom_size(0x300000, 0x100000)
                length2 = 0x200000 if length2 >= 0x80000 else 0x100000
            if length2 > 0x80000:
                length += length2
        return length

    def ram_available(self) -> bool:
        self.device.write_u16(TIME_REGISTER, 0xFFFF)
        original = self.device.read_u16(SRAM_BASE)
        self.device.write_u16(SRAM_BASE, original ^ 0xFFFF)
        observed = self.device.read_u16(SRAM_BASE)
        self.device.write_u16(SRAM_BASE, original)
        return (observed & 0xFF) == ((original & 0xFF) ^ 0xFF)

    def ram_size(self) -> int:
        if not self.ram_available():
            return 0
        first = self.device.read_u16(SRAM_BASE) & 0xFF
        size = 256
        while size < 0x100000:
            address = SRAM_BASE + size
            original = self.device.read_u16(address)
            self.device.write_u16(address, original ^ 0xFFFF)
            observed = self.device.read_u16(address)
            first_after = self.device.read_u16(SRAM_BASE) & 0xFF
            self.device.write_u16(address, original)
            if (observed & 0xFF) != ((original & 0xFF) ^ 0xFF):
                break
            if first_after != first:
                break
            size *= 2
        return size

    def _detect_extra_rom(self) -> bool:
        self.device.write_u16(TIME_REGISTER, 0)
        self.device.set_address(SRAM_BASE)
        first = self.device.read(512)
        self.device.set_address(SRAM_BASE)
        sector = self.device.read(512)
        if sector != first:
            return False
        self.device.set_address(SRAM_BASE + 0x10000)
        self.device.read(512)
        self.device.write_u16(TIME_REGISTER, 0)
        self.device.set_address(SRAM_BASE)
        return self.device.read(512) != first

    def _check_rom_size(self, base: int, maximum: int) -> int:
        self.device.write_u16(TIME_REGISTER, 0)
        self.device.set_address(base)
        first = self.device.read(512)
        length = 0x8000
        while True:
            self.device.set_address(base + length)
            if self.device.read(512) == first:
                break
            length *= 2
            if length >= maximum:
                break
        return 0 if length == 0x8000 else length


def check_cart(port: str) -> str:
    with FlashKitDevice(port) as device:
        device.check_for_flash()
        return Cartridge(device).rom_name()


def cart_layout(port: str) -> dict[str, int | str]:
    with FlashKitDevice(port) as device:
        device.check_for_flash()
        cart = Cartridge(device)
        try:
            return {
                "name": cart.rom_name(),
                "header_size": cart.rom_size(trust_header=True),
                "addressable_size": cart.rom_size(trust_header=False),
                "ram_size": cart.ram_size(),
            }
        finally:
            device.write_u16(TIME_REGISTER, 0)


def read_rom(port: str, output: str | Path, size: int | None = None) -> tuple[int, str]:
    with FlashKitDevice(port) as device:
        device.set_delay(1)
        cart = Cartridge(device)
        rom_size = size if size is not None else cart.rom_size(trust_header=True)
        if rom_size < 2 or rom_size > MAX_ROM_SIZE:
            raise FlashKitError(f"Invalid ROM read size: {rom_size} bytes")
        if rom_size % 2:
            rom_size += 1
        device.write_u16(TIME_REGISTER, 0)
        device.set_address(0)
        digest = hashlib.md5()
        remaining = rom_size
        with open(output, "wb") as rom_file:
            while remaining:
                block = device.read(min(READ_BLOCK_SIZE, remaining))
                rom_file.write(block)
                digest.update(block)
                remaining -= len(block)
        return rom_size, digest.hexdigest()


def _prepare_special_mapper_image(device: FlashKitDevice, rom: bytes) -> bytes:
    start = CART_METADATA_SIGNATURE_OFFSET
    end = start + len(CART_METADATA_SIGNATURE)
    if rom[start:end] != CART_METADATA_SIGNATURE:
        return rom

    device.write_u16(CART_METADATA_BANK_REGISTER, CART_METADATA_BANK_VALUE)
    now = datetime.now()
    date = now.day | (now.month << 5) | ((now.year - 1980) << 9)
    clock = (now.second // 2) | (now.minute << 5) | (now.hour << 11)
    stamp = bytes((date & 0xFF, (date >> 8) & 0xFF,
                   clock & 0xFF, (clock >> 8) & 0xFF))
    return rom[:CART_METADATA_DATETIME_OFFSET] + stamp + rom[CART_METADATA_DATETIME_OFFSET + 4:]


def write_rom(
    port: str,
    image_path: str | Path,
    prepared_output: str | Path | None = None,
    max_size: int = 4 * 1024 * 1024,
) -> tuple[int, str]:
    rom = Path(image_path).read_bytes()
    if not rom:
        raise FlashKitError("ROM image is empty")
    if max_size < 0:
        raise ValueError("--max-size must be zero or a positive number of bytes")
    padded_size = ((len(rom) + ROM_SIZE_MULTIPLE - 1) // ROM_SIZE_MULTIPLE) * ROM_SIZE_MULTIPLE
    if max_size and padded_size > max_size:
        raise FlashKitError(
            f"ROM pads to {padded_size} bytes; configured software limit is {max_size} bytes"
        )
    rom += bytes(padded_size - len(rom))

    with FlashKitDevice(port) as device:
        try:
            device.set_delay(0)
            device.check_for_flash()
            rom = _prepare_special_mapper_image(device, rom)
            if prepared_output:
                Path(prepared_output).write_bytes(rom)

            device.flash_reset_bypass()
            for offset in range(0, len(rom), ROM_SIZE_MULTIPLE):
                print(f"\rErase: {offset * 100 / len(rom):5.1f}%", end="", flush=True)
                device.flash_erase(offset)
            print("\rErase: 100.0%", flush=True)

            device.flash_unlock_bypass()
            device.set_address(0)
            for offset in range(0, len(rom), WRITE_BLOCK_SIZE):
                device.flash_write(rom[offset:offset + WRITE_BLOCK_SIZE])
                if offset % (256 * 1024) == 0:
                    print(f"\rWrite: {offset * 100 / len(rom):5.1f}%", end="", flush=True)
            print("\rWrite: 100.0%", flush=True)

            device.flash_reset_bypass()
            device.set_address(0)
            readback = bytearray()
            for offset in range(0, len(rom), WRITE_BLOCK_SIZE):
                readback.extend(device.read(WRITE_BLOCK_SIZE))
                if offset % (256 * 1024) == 0:
                    print(f"\rVerify: {offset * 100 / len(rom):5.1f}%", end="", flush=True)
            if readback != rom:
                mismatch = next(i for i, (left, right) in enumerate(zip(readback, rom)) if left != right)
                raise FlashKitError(f"Flash verify failed at byte offset {mismatch}")
            print("\rVerify: 100.0%", flush=True)
            return len(rom), hashlib.md5(rom).hexdigest()
        except Exception:
            try:
                device.flash_reset_bypass()
            except Exception:
                pass
            raise
