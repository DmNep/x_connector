"""Запись агентной части x_connector на USB-флешку.

На ноутбуке (интернет можно): py -m xconn_channel stick E:\\
На сервере (сети нет): sudo sh install.sh с этой флешки.

На носитель попадает пакет xconn_channel, POSIX-установщик и unit
systemd. pip/apt с сервера не вызываются (AGENTS.md 2.5, 3.2, 3.3, 3.7).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import __version__

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGING = _ROOT / "packaging"
_PACKAGE = Path(__file__).resolve().parent

_STICK_FILES = (
    "install.sh",
    "run-agent.sh",
    "xconn-agent.service",
    "README.stick.txt",
)

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.pyd")


class StickError(Exception):
    """Некуда писать или в дереве нет packaging/."""


def list_removable() -> list[str]:
    """Буквы съёмных дисков Windows. На других ОС — пусто."""
    if os.name != "nt":
        return []
    import ctypes

    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = f"{chr(ord('A') + i)}:\\"
        if kernel32.GetDriveTypeW(letter) == 2:  # DRIVE_REMOVABLE
            drives.append(letter)
    return drives


def _copy_text_lf(src: Path, dest: Path) -> None:
    """Скрипты на флешку строго с LF: sh на Linux не любит CRLF."""
    text = src.read_text(encoding="utf-8")
    dest.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def write_stick(dest: str | os.PathLike[str]) -> Path:
    """Скопировать агент и установщик в dest. Возвращает Path назначения."""
    target = Path(dest)
    if not _PACKAGING.is_dir():
        raise StickError(f"нет каталога packaging/: {_PACKAGING}")
    if not _PACKAGE.is_dir():
        raise StickError(f"нет пакета xconn_channel: {_PACKAGE}")
    target.mkdir(parents=True, exist_ok=True)
    pkg_dest = target / "xconn_channel"
    if pkg_dest.exists():
        shutil.rmtree(pkg_dest)
    shutil.copytree(_PACKAGE, pkg_dest, ignore=_IGNORE)
    for name in _STICK_FILES:
        src = _PACKAGING / name
        if not src.is_file():
            raise StickError(f"нет файла {src}")
        out_name = "README.txt" if name == "README.stick.txt" else name
        _copy_text_lf(src, target / out_name)
    debs_src = _PACKAGING / "python-debs"
    debs_dest = target / "python-debs"
    if debs_src.is_dir():
        if debs_dest.exists():
            shutil.rmtree(debs_dest)
        shutil.copytree(debs_src, debs_dest, ignore=_IGNORE)
    (target / "VERSION").write_text(__version__ + "\n", encoding="utf-8", newline="\n")
    return target


def write_report(dest: Path, stream=None) -> None:
    out = stream if stream is not None else sys.stdout
    encoding = getattr(out, "encoding", None) or "utf-8"
    lines = [
        f"флешка: {dest}",
        f"версия: {__version__}",
        "на сервере: sudo sh install.sh",
        "",
    ]
    text = "\n".join(lines)
    data = text.encode(encoding, errors="replace")
    buf = getattr(out, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        out.write(text)
