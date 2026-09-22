"""Запись агентной части x_connector на USB-флешку.

На ноутбуке (интернет можно): py -m xconn_channel stick E:\\
На сервере (сети нет): sudo sh install.sh с этой флешки.

На носитель попадают пакет xconn_channel, POSIX-установщик, unit
systemd и автономный Linux CPython (x86_64 и aarch64). pip/apt с
сервера не вызываются (AGENTS.md 2.5, 3.2, 3.3, 3.7).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGING = _ROOT / "packaging"
_PACKAGE = Path(__file__).resolve().parent
_PYTHON_CACHE = _PACKAGING / "python-linux"

_STICK_FILES = (
    "install.sh",
    "run-agent.sh",
    "xconn-agent.service",
    "README.stick.txt",
)

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.pyd")

_USER_AGENT = "x_connector-stick/0.3 (+https://github.com/DmNep/x_connector)"
_CHUNK = 64 * 1024


class StickError(Exception):
    """Некуда писать, нет packaging/ или не скачался Linux Python."""


@dataclass(frozen=True)
class LinuxPython:
    """Один архив python-build-standalone для флешки."""

    arch: str
    filename: str
    url: str
    sha256: str
    size: int


# Релиз astral-sh/python-build-standalone, install_only_stripped, glibc 2.17+.
# Не «latest»: имя и хеш фиксированы, чтобы повторная запись флешки сходилась.
PYTHON_ASSETS: tuple[LinuxPython, ...] = (
    LinuxPython(
        arch="x86_64",
        filename=(
            "cpython-3.12.14+20260901-x86_64-unknown-linux-gnu-"
            "install_only_stripped.tar.gz"
        ),
        url=(
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20260901/cpython-3.12.14%2B20260901-x86_64-unknown-"
            "linux-gnu-install_only_stripped.tar.gz"
        ),
        sha256="72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c",
        size=34143368,
    ),
    LinuxPython(
        arch="aarch64",
        filename=(
            "cpython-3.12.14+20260901-aarch64-unknown-linux-gnu-"
            "install_only_stripped.tar.gz"
        ),
        url=(
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20260901/cpython-3.12.14%2B20260901-aarch64-unknown-"
            "linux-gnu-install_only_stripped.tar.gz"
        ),
        sha256="577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d",
        size=29199399,
    ),
)


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def cached_python_archives(
    cache: Path | None = None,
    assets: tuple[LinuxPython, ...] | None = None,
) -> list[Path]:
    """Архивы в кэше ноутбука, которые уже прошли проверку имени."""
    root = cache if cache is not None else _PYTHON_CACHE
    wanted = assets if assets is not None else PYTHON_ASSETS
    if not root.is_dir():
        return []
    names = {asset.filename for asset in wanted}
    found = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name in names:
            found.append(path)
    return found


def _asset_ready(asset: LinuxPython, dest: Path) -> bool:
    if not dest.is_file():
        return False
    if dest.stat().st_size != asset.size:
        return False
    return _sha256_file(dest) == asset.sha256


def _download_asset(
    asset: LinuxPython,
    dest: Path,
    opener=None,
    log=None,
) -> None:
    """Скачать один архив во временный файл и атомарно переименовать."""
    if opener is None:
        def open_url(req: urllib.request.Request):
            return urllib.request.urlopen(req, timeout=60)
    else:
        open_url = opener
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(asset.url, headers={"User-Agent": _USER_AGENT})
    if log is not None:
        log(f"скачиваю Linux python3 {asset.arch} ({asset.size} байт)")
    digest = hashlib.sha256()
    total = 0
    try:
        with open_url(request) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    except (urllib.error.URLError, OSError) as exc:
        if tmp.exists():
            tmp.unlink()
        raise StickError(f"не скачался {asset.filename}: {exc}") from exc
    if total != asset.size or digest.hexdigest() != asset.sha256:
        tmp.unlink(missing_ok=True)
        raise StickError(
            f"хеш или размер не совпали: {asset.filename} "
            f"({total} байт, sha256 {digest.hexdigest()})"
        )
    tmp.replace(dest)


def ensure_linux_python(
    cache: Path | None = None,
    opener=None,
    log=None,
    assets: tuple[LinuxPython, ...] | None = None,
) -> list[Path]:
    """Скачать оба архива Linux CPython в кэш, если их ещё нет."""
    root = cache if cache is not None else _PYTHON_CACHE
    wanted = assets if assets is not None else PYTHON_ASSETS
    root.mkdir(parents=True, exist_ok=True)
    ready: list[Path] = []
    for asset in wanted:
        dest = root / asset.filename
        if _asset_ready(asset, dest):
            ready.append(dest)
            continue
        _download_asset(asset, dest, opener=opener, log=log)
        ready.append(dest)
    return ready


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=_IGNORE)


def _copy_linux_python(target: Path, cache: Path | None = None) -> list[str]:
    """Скопировать архивы на флешку. Возвращает имена файлов."""
    archives = cached_python_archives(cache)
    dest = target / "python-linux"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in archives:
        shutil.copy2(src, dest / src.name)
        copied.append(src.name)
    readme = _PYTHON_CACHE / "README.txt"
    if readme.is_file():
        _copy_text_lf(readme, dest / "README.txt")
    return copied


def write_stick(
    dest: str | os.PathLike[str],
    cache: Path | None = None,
) -> Path:
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
        _copy_tree(debs_src, debs_dest)
    alsa_src = _PACKAGING / "alsa-debs"
    alsa_dest = target / "alsa-debs"
    if alsa_src.is_dir():
        _copy_tree(alsa_src, alsa_dest)
    _copy_linux_python(target, cache=cache)
    (target / "VERSION").write_text(__version__ + "\n", encoding="utf-8", newline="\n")
    return target


def write_report(dest: Path, stream=None) -> None:
    out = stream if stream is not None else sys.stdout
    encoding = getattr(out, "encoding", None) or "utf-8"
    py_dir = dest / "python-linux"
    archives = []
    if py_dir.is_dir():
        archives = sorted(
            p.name for p in py_dir.iterdir() if p.suffixes[-2:] == [".tar", ".gz"]
        )
    lines = [
        f"флешка: {dest}",
        f"версия: {__version__}",
        "на сервере: sudo sh install.sh",
        "",
    ]
    if archives:
        lines.insert(2, "linux python: " + ", ".join(archives))
    else:
        lines.insert(2, "linux python: нет - на ноутбуке повторите stick без --no-fetch")
    text = "\n".join(lines)
    data = text.encode(encoding, errors="replace")
    buf = getattr(out, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        out.write(text)
