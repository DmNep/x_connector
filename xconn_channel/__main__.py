"""Точка входа: py -m xconn_channel <команда>.

Клиент — удобная программа для ИИ-агента на ноутбуке: отправить команду,
напечатать сетку терминала. Агент на сервере слушает звуковую карту.
loopback — оба конца в одном процессе, без кабеля, для отладки.
stick — записать агент и установщик на USB-флешку.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from . import __version__, config
from .audioio import open_audio
from .client import Client
from .handshake import HandshakeError
from .session import SessionError
from .devcheck import (
    DeviceError,
    check_winmm,
    emit,
    explain_oserror,
    parse_winmm_index,
    report_devices,
)
from .host import AgentHost
from .shell import open_shell
from .stick import StickError, ensure_linux_python, list_removable, write_report, write_stick
from .transport import AudioTransport, SampleLink


def _print_screen(client: Client) -> None:
    text = client.render()
    if not text:
        return
    # Сетка cp437, консоль Windows часто cp1251: печатаем с заменой,
    # а не падаем на псевдографике cmd.exe.
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    payload = (text + "\n").encode(encoding, errors="replace")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(payload)
        buf.flush()
    else:
        sys.stdout.write(payload.decode(encoding, errors="replace"))
        sys.stdout.flush()


def _run_commands(client: Client, commands: list[str], repl: bool) -> int:
    for command in commands:
        try:
            client.cmd(command)
        except SessionError as exc:
            emit(str(exc))
            _print_screen(client)
            return 1
        _print_screen(client)
    if repl:
        try:
            while True:
                line = input("xconn> ")
                if line.strip() in (".quit", ".exit"):
                    break
                if line.startswith(".key "):
                    client.key(line[5:].strip())
                elif line.startswith(".put "):
                    parts = line.split()
                    if len(parts) < 2:
                        print("usage: .put LOCAL [NAME]")
                        continue
                    remote = parts[2] if len(parts) > 2 else None
                    client.put(parts[1], remote)
                    print("put ok")
                    continue
                elif line.startswith(".get "):
                    parts = line.split()
                    if len(parts) < 2:
                        print("usage: .get REMOTE [LOCAL]")
                        continue
                    local = parts[2] if len(parts) > 2 else parts[1].replace("\\", "/").rsplit("/", 1)[-1]
                    try:
                        client.get(parts[1], local)
                    except SessionError as exc:
                        print(exc)
                        continue
                    print("get ok", local)
                    continue
                elif line.startswith(".ping"):
                    client.ping()
                    print("pong")
                    continue
                elif line.startswith(".refresh"):
                    try:
                        client.refresh()
                    except SessionError as exc:
                        print(exc)
                        continue
                elif line.startswith(".resize "):
                    parts = line.split()
                    if len(parts) != 3:
                        print("usage: .resize ROWS COLS")
                        continue
                    try:
                        client.resize(int(parts[1]), int(parts[2]))
                    except (ValueError, SessionError) as exc:
                        print(exc)
                        continue
                else:
                    try:
                        client.cmd(line)
                    except SessionError as exc:
                        print(exc)
                        continue
                _print_screen(client)
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
    return 0


def _cmd_loopback(args: argparse.Namespace) -> int:
    link = SampleLink()
    sink_c, source_c = link.end_a()
    sink_a, source_a = link.end_b()
    client_tr = AudioTransport(sink_c, source_c, config.PROBE)
    agent_tr = AudioTransport(sink_a, source_a, config.PROBE)
    argv = args.shell or None
    pty = open_shell(argv)
    host = AgentHost(
        agent_tr.send,
        agent_tr.receive,
        pty,
        transport=agent_tr,
    )
    stop = threading.Event()

    def agent_loop() -> None:
        host.serve(stop.is_set)

    thread = threading.Thread(target=agent_loop, daemon=True)
    thread.start()
    try:
        client = Client(client_tr.send, client_tr.receive, transport=client_tr)
        client.connect()
        return _run_commands(client, args.command, args.repl)
    finally:
        stop.set()
        thread.join(timeout=1.0)
        if hasattr(pty, "close"):
            pty.close()


def _open_transport(args: argparse.Namespace, role: str) -> tuple:
    del role  # одинаковые kwargs на обоих концах: capture=вход, playback=выход
    kwargs: dict = {}
    name = args.backend
    if name is None:
        name = "winmm" if os.name == "nt" else "alsa"
    if name == "alsa":
        kwargs["capture_device"] = args.capture or "hw:0,0"
        kwargs["playback_device"] = args.playback or "hw:0,0"
    elif name == "winmm":
        kwargs["in_device"] = parse_winmm_index(args.capture, "capture")
        kwargs["out_device"] = parse_winmm_index(args.playback, "playback")
        check_winmm(kwargs["in_device"], kwargs["out_device"])
    elif name == "wav":
        # --capture/--playback переиспользуются как пути к WAV-файлам:
        # вход и выход у WavAudio, а не устройство (docs/protocol.md 2).
        kwargs["in_path"] = args.capture
        kwargs["out_path"] = args.playback
    device = open_audio(args.backend, **kwargs)
    transport = AudioTransport(device.sink, device.source, config.PROBE)
    return device, transport


def _cmd_client(args: argparse.Namespace) -> int:
    try:
        device, transport = _open_transport(args, "client")
    except DeviceError as exc:
        emit(str(exc))
        return 2
    except OSError as exc:
        emit(explain_oserror(exc))
        return 2
    try:
        client = Client(transport.send, transport.receive, transport=transport)
        try:
            client.connect()
        except (SessionError, HandshakeError) as exc:
            emit(str(exc))
            return 1
        return _run_commands(client, args.command, args.repl)
    finally:
        device.close()


def _cmd_stick(args: argparse.Namespace) -> int:
    dest = args.dest
    if not dest:
        drives = list_removable()
        if drives:
            sys.stderr.write("укажите флешку, съёмные диски:\n")
            for letter in drives:
                sys.stderr.write(f"  {letter}\n")
        else:
            sys.stderr.write(
                "укажите путь: py -m xconn_channel stick E:\\\n"
            )
        return 2
    try:
        if not args.no_fetch:
            ensure_linux_python(log=lambda msg: sys.stderr.write(msg + "\n"))
        written = write_stick(dest)
    except StickError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"не записалось: {exc}\n")
        return 2
    write_report(written)
    return 0


def _cmd_agent(args: argparse.Namespace) -> int:
    if os.name == "nt" and args.backend not in ("wav",):
        sys.stderr.write(
            "агент рассчитан на Linux (ALSA hw:). "
            "На Windows: py -m xconn_channel loopback\n"
        )
        return 2
    try:
        device, transport = _open_transport(args, "agent")
    except DeviceError as exc:
        emit(str(exc))
        return 2
    except OSError as exc:
        emit(explain_oserror(exc))
        return 2
    argv = args.shell or None
    pty = open_shell(argv)
    host = AgentHost(
        transport.send,
        transport.receive,
        pty,
        transport=transport,
    )
    try:
        host.serve()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if hasattr(pty, "close"):
            pty.close()
        device.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xconn_channel",
        description="Звуковой канал x_connector: клиент, агент, loopback, флешка.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"xconn_channel {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-c",
            "--command",
            action="append",
            default=[],
            help="команда на сервер (повторяемый флаг)",
        )
        p.add_argument("--repl", action="store_true", help="читать команды с stdin")
        p.add_argument(
            "--backend",
            choices=("winmm", "alsa", "wav"),
            default=None,
            help="аудио-обвязка, по умолчанию по ОС",
        )
        p.add_argument(
            "--capture",
            default=None,
            help="вход: hw:N,M / номер winmm / путь к WAV-файлу при --backend wav",
        )
        p.add_argument(
            "--playback",
            default=None,
            help="выход: hw:N,M / номер winmm / путь к WAV-файлу при --backend wav",
        )
        p.add_argument(
            "--shell",
            nargs=argparse.REMAINDER,
            help="argv оболочки агента, всё после --shell",
        )

    p_loop = sub.add_parser("loopback", help="клиент и агент в одном процессе")
    add_common(p_loop)
    p_loop.set_defaults(func=_cmd_loopback)

    p_client = sub.add_parser("client", help="клиент на ноутбуке, звуковая карта")
    add_common(p_client)
    p_client.set_defaults(func=_cmd_client)

    p_agent = sub.add_parser("agent", help="агент на сервере, ALSA hw:")
    add_common(p_agent)
    p_agent.set_defaults(func=_cmd_agent)

    p_stick = sub.add_parser("stick", help="записать агент на USB-флешку")
    p_stick.add_argument(
        "dest",
        nargs="?",
        default=None,
        help="корень флешки, например E:\\",
    )
    p_stick.add_argument(
        "--no-fetch",
        action="store_true",
        help="не скачивать Linux python3, только то что уже в кэше",
    )
    p_stick.set_defaults(func=_cmd_stick)

    p_devices = sub.add_parser("devices", help="список входов и выходов")
    p_devices.set_defaults(func=lambda _args: report_devices())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
