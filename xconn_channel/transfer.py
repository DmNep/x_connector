"""Передача файла клиент → агент: FILE_OPEN / FILE_DATA / FILE_CLOSE.

docs/protocol.md 7. Без сети, без внешних библиотек: CRC-32 из zlib.
Имя — только basename, без «..» и разделителей пути: агент пишет в
свой каталог-приёмник, не куда скажет клиент.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

from . import config


class TransferError(ValueError):
    """Имя, размер или кадр неверны. Агент отвечает NAK_STATE / NAK_LENGTH."""


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC, как zlib.crc32: совместим с cksum -o 3 на Linux."""
    return zlib.crc32(data) & 0xFFFFFFFF


def sanitize_name(name: str) -> str:
    """Только имя файла в каталоге-приёмнике, не путь."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not base or base in (".", "..") or "\x00" in base:
        raise TransferError(f"непригодное имя {name!r}")
    raw = base.encode("utf-8")
    if len(raw) > config.FILE_NAME_BYTES:
        raise TransferError(
            f"имя {len(raw)} байт, лимит {config.FILE_NAME_BYTES}"
        )
    return base


def encode_open(name: str, size: int, digest: int) -> bytes:
    """FILE_OPEN: имя 64 байта NUL-pad, size u32 BE, crc32 u32 BE."""
    clean = sanitize_name(name)
    if not 0 <= size <= config.FILE_MAX_BYTES:
        raise TransferError(f"размер {size} вне 0..{config.FILE_MAX_BYTES}")
    if not 0 <= digest <= 0xFFFFFFFF:
        raise TransferError("crc32 вне uint32")
    raw = clean.encode("utf-8")
    padded = raw + b"\x00" * (config.FILE_NAME_BYTES - len(raw))
    return padded + struct.pack(">II", size, digest)


def decode_open(payload: bytes) -> tuple[str, int, int]:
    need = config.FILE_NAME_BYTES + 8
    if len(payload) != need:
        raise TransferError(f"FILE_OPEN длина {len(payload)}, ждут {need}")
    raw = payload[: config.FILE_NAME_BYTES]
    name = raw.split(b"\x00", 1)[0].decode("utf-8")
    size, digest = struct.unpack(">II", payload[config.FILE_NAME_BYTES :])
    return sanitize_name(name), size, digest


def encode_data(offset: int, chunk: bytes) -> bytes:
    """FILE_DATA: offset u32 BE, length u16 BE, байты."""
    if not 0 <= offset <= config.FILE_MAX_BYTES:
        raise TransferError(f"смещение {offset} вне диапазона")
    if len(chunk) > config.FILE_CHUNK:
        raise TransferError(
            f"кусок {len(chunk)} байт, лимит {config.FILE_CHUNK}"
        )
    return struct.pack(">IH", offset, len(chunk)) + chunk


def decode_data(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < config.FILE_DATA_HDR:
        raise TransferError("FILE_DATA короче заголовка")
    offset, length = struct.unpack(">IH", payload[: config.FILE_DATA_HDR])
    chunk = payload[config.FILE_DATA_HDR :]
    if len(chunk) != length:
        raise TransferError(
            f"FILE_DATA length={length}, фактически {len(chunk)}"
        )
    return offset, chunk


def encode_close(digest: int) -> bytes:
    if not 0 <= digest <= 0xFFFFFFFF:
        raise TransferError("crc32 вне uint32")
    return struct.pack(">I", digest)


def decode_close(payload: bytes) -> int:
    if len(payload) != 4:
        raise TransferError(f"FILE_CLOSE длина {len(payload)}, ждут 4")
    (digest,) = struct.unpack(">I", payload)
    return digest


def dest_path(root: str | os.PathLike[str], name: str) -> Path:
    """Конечный путь в root. sanitize_name уже отсёк обход каталога."""
    clean = sanitize_name(name)
    base = Path(root).resolve()
    path = (base / clean).resolve()
    if base != path and base not in path.parents:
        raise TransferError(f"путь ушёл из приёмника: {path}")
    return path


def iter_chunks(data: bytes, size: int = config.FILE_CHUNK):
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + size]
        yield offset, chunk
        offset += len(chunk)
