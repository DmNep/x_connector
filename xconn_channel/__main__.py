"""Точка входа: py -m xconn_channel <команда>.

Клиент — удобная программа для ИИ-агента на ноутбуке: отправить команду,
напечатать сетку терминала. Агент на сервере слушает звуковую карту.
loopback — оба конца в одном процессе, без кабеля, для отладки.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from . import __version__, config
from .audioio import open_audio
from .client import Client
from .host import AgentHost
from .shell import open_shell
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
        client.cmd(command)
        _print_screen(client)
    if repl:
        try:
            while True:
                line = input("xconn> ")
                if line.strip() in (".quit", ".exit"):
                    break
                if line.startswith(".key "):
                    client.key(line[5:].strip())
                elif line.startswith(".ping"):
                    client.ping()
                    print("pong")
                    continue
                elif line.startswith(".refresh"):
                    client.refresh()
                else:
                    client.cmd(line)
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
        try:
            kwargs["in_device"] = int(args.capture) if args.capture is not None else -1
        except ValueError:
            kwargs["in_device"] = -1
        try:
            kwargs["out_device"] = int(args.playback) if args.playback is not None else -1
        except ValueError:
            kwargs["out_device"] = -1
    elif name == "wav":
        # --capture/--playback переиспользуются как пути к WAV-файлам:
        # вход и выход у WavAudio, а не устройство (docs/protocol.md 2).
        kwargs["in_path"] = args.capture
        kwargs["out_path"] = args.playback
    device = open_audio(args.backend, **kwargs)
    transport = AudioTransport(device.sink, device.source, config.PROBE)
    return device, transport


def _cmd_client(args: argparse.Namespace) -> int:
    device, transport = _open_transport(args, "client")
    try:
        client = Client(transport.send, transport.receive, transport=transport)
        client.connect()
        return _run_commands(client, args.command, args.repl)
    finally:
        device.close()


def _cmd_agent(args: argparse.Namespace) -> int:
    if os.name == "nt" and args.backend not in ("wav",):
        sys.stderr.write(
            "агент рассчитан на Linux (ALSA hw:). "
            "На Windows: py -m xconn_channel loopback\n"
        )
        return 2
    device, transport = _open_transport(args, "agent")
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
        description="Звуковой канал x_connector: клиент, агент, loopback.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
