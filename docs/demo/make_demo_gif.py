"""Генератор демо-gif: как выглядит обмен x_connector (сигнал + ответный экран).

Иллюстративная схема, не буквальная осциллограмма: тона упрощены для
наглядности на малом разрешении, на реальном железе ничего не
записывалось (AGENTS.md 4, «На железе не гонялось»). Сюжет и тайминги
(REQ -> RESP -> ACK, GAP, дельта/полный снимок) соответствуют
docs/protocol.md 8.1, 8.2, 6.2.

Запуск: py -m pip install pillow && py docs/demo/make_demo_gif.py
Результат — docs/demo/x_connector_demo.gif, рядом со скриптом.
Требует шрифты Windows (Consolas, Segoe UI); на другой ОС поправьте
FONTS_DIR и имена файлов шрифтов.
SPEED_FACTOR ниже — общий множитель скорости (0.5 — вдвое медленнее).
"""

import math
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 980, 700
BG = (10, 14, 20)
PANEL_BG = (16, 22, 30)
WIRE_BG = (22, 28, 38)
CLIENT_COLOR = (86, 182, 255)   # ноутбук -> сервер (REQ, ACK)
AGENT_COLOR = (255, 176, 74)    # сервер -> ноутбук (RESP)
IDLE_COLOR = (48, 56, 68)
TERM_BG = (8, 10, 8)
TERM_GREEN = (120, 230, 130)
TERM_DIM = (70, 140, 80)
TERM_PROMPT = (140, 200, 255)
WHITE = (226, 232, 240)
GRAY = (140, 150, 165)

FONTS_DIR = r"C:\Windows\Fonts"
f_title = ImageFont.truetype(FONTS_DIR + r"\segoeuib.ttf", 23)
f_sub = ImageFont.truetype(FONTS_DIR + r"\segoeui.ttf", 14)
f_label = ImageFont.truetype(FONTS_DIR + r"\segoeuib.ttf", 15)
f_small = ImageFont.truetype(FONTS_DIR + r"\segoeui.ttf", 13)
f_status = ImageFont.truetype(FONTS_DIR + r"\consolab.ttf", 16)
f_term = ImageFont.truetype(FONTS_DIR + r"\consola.ttf", 16)
f_term_b = ImageFont.truetype(FONTS_DIR + r"\consolab.ttf", 16)

LAPTOP_X, LAPTOP_Y = 70, 88
SERVER_X, SERVER_Y = 830, 88
WIRE_X0, WIRE_X1 = 190, 810
WIRE_TOP_Y = 234
WIRE_BOT_Y = 284
TERM_X0, TERM_Y0 = 60, 360
TERM_X1, TERM_Y1 = 920, 660
ROW_H = 20
COLS_VISIBLE = 72
ROWS_VISIBLE = 12


def draw_laptop(d: ImageDraw.ImageDraw, x: int, y: int) -> None:
    d.rounded_rectangle([x, y, x + 90, y + 58], radius=4, outline=GRAY, width=2)
    d.rectangle([x + 6, y + 6, x + 84, y + 46], fill=(4, 8, 14))
    d.rounded_rectangle([x - 10, y + 58, x + 100, y + 66], radius=3, outline=GRAY, width=2)


def draw_server(d: ImageDraw.ImageDraw, x: int, y: int) -> None:
    for i in range(3):
        top = y + i * 22
        d.rounded_rectangle([x, top, x + 90, top + 16], radius=2, outline=GRAY, width=2)
        d.ellipse([x + 6, top + 6, x + 10, top + 10], fill=(90, 220, 130))


def wave_points(x0: int, x1: int, y_mid: int, amp: float, seed_bits: str, phase_px: int):
    """Стилизованная FSK-волна: сегменты по битам seed_bits, mark гуще, space реже."""
    pts = []
    n = len(seed_bits)
    seg_w = (x1 - x0) / max(1, n)
    for i, bit in enumerate(seed_bits):
        cycles = 3.2 if bit == "1" else 5.6
        sx0 = x0 + i * seg_w
        steps = 22
        for s in range(steps + 1):
            frac = s / steps
            x = sx0 + frac * seg_w
            if x < x0 + phase_px:
                continue
            ang = frac * cycles * 2 * math.pi
            y = y_mid - amp * math.sin(ang)
            pts.append((x, y))
    return pts


def draw_wire(
    d: ImageDraw.ImageDraw,
    y_mid: int,
    label: str,
    active: bool,
    color,
    bits: str,
    reveal: float,
) -> None:
    d.line([(WIRE_X0 - 14, y_mid), (WIRE_X1 + 14, y_mid)], fill=WIRE_BG, width=18)
    d.text((WIRE_X0 - 14, y_mid - 34), label, font=f_small, fill=GRAY)
    if not active:
        d.line([(WIRE_X0, y_mid), (WIRE_X1, y_mid)], fill=IDLE_COLOR, width=2)
        return
    x_cut = int(WIRE_X0 + (WIRE_X1 - WIRE_X0) * reveal)
    pts = wave_points(WIRE_X0, x_cut, y_mid, 16, bits, 0)
    if len(pts) >= 2:
        d.line(pts, fill=color, width=3, joint="curve")
    if x_cut < WIRE_X1:
        d.line([(x_cut, y_mid), (WIRE_X1, y_mid)], fill=IDLE_COLOR, width=2)
    if reveal < 1.0:
        hx = pts[-1][0] if pts else WIRE_X0
        hy = pts[-1][1] if pts else y_mid
        d.ellipse([hx - 5, hy - 5, hx + 5, hy + 5], fill=color)


def draw_header(d: ImageDraw.ImageDraw) -> None:
    d.text((60, 18), "x_connector \u2014 \u0430\u0443\u0434\u0438\u043e\u043a\u0430\u043d\u0430\u043b \u043d\u043e\u0443\u0442\u0431\u0443\u043a \u2194 \u0441\u0435\u0440\u0432\u0435\u0440", font=f_title, fill=WHITE)
    d.text(
        (60, 46),
        "CPFSK Bell 202 \u00b7 \u043f\u043e\u043b\u0443\u0434\u0443\u043f\u043b\u0435\u043a\u0441 \u00b7 \u0441\u0445\u0435\u043c\u0430 \u0442\u043e\u043d\u043e\u0432 \u0443\u043f\u0440\u043e\u0449\u0435\u043d\u0430 \u0434\u043b\u044f \u043d\u0430\u0433\u043b\u044f\u0434\u043d\u043e\u0441\u0442\u0438",
        font=f_sub,
        fill=GRAY,
    )
    draw_laptop(d, LAPTOP_X, LAPTOP_Y)
    d.text((LAPTOP_X - 8, LAPTOP_Y + 72), "\u043d\u043e\u0443\u0442\u0431\u0443\u043a (\u043a\u043b\u0438\u0435\u043d\u0442)", font=f_label, fill=WHITE)
    d.text((LAPTOP_X - 8, LAPTOP_Y + 92), "\u0418\u0418-\u0430\u0433\u0435\u043d\u0442", font=f_small, fill=GRAY)
    draw_server(d, SERVER_X, SERVER_Y)
    d.text((SERVER_X - 6, SERVER_Y + 72), "\u0441\u0435\u0440\u0432\u0435\u0440 (\u0430\u0433\u0435\u043d\u0442)", font=f_label, fill=WHITE)
    d.text((SERVER_X - 6, SERVER_Y + 92), "Linux, \u0431\u0435\u0437 \u0441\u0435\u0442\u0438", font=f_small, fill=GRAY)


def draw_status(d: ImageDraw.ImageDraw, text: str, color) -> None:
    tw = d.textlength(text, font=f_status)
    cx = W / 2
    d.rounded_rectangle(
        [cx - tw / 2 - 14, 316, cx + tw / 2 + 14, 344], radius=6, fill=(20, 26, 36)
    )
    d.text((cx - tw / 2, 320), text, font=f_status, fill=color)


def draw_terminal(d: ImageDraw.ImageDraw, lines, cursor_row, cursor_col, cursor_on) -> None:
    d.rounded_rectangle([TERM_X0, TERM_Y0, TERM_X1, TERM_Y1], radius=8, fill=TERM_BG, outline=(40, 46, 56), width=1)
    d.rounded_rectangle([TERM_X0, TERM_Y0, TERM_X1, TERM_Y0 + 26], radius=8, fill=(24, 28, 34))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([TERM_X0 + 12 + i * 18, TERM_Y0 + 9, TERM_X0 + 20 + i * 18, TERM_Y0 + 17], fill=c)
    d.text(
        (TERM_X0 + 80, TERM_Y0 + 5),
        "\u044d\u043a\u0440\u0430\u043d \u0441\u0435\u0440\u0432\u0435\u0440\u0430 \u2014 \u0432\u0438\u0434\u0438\u0442 \u043a\u043b\u0438\u0435\u043d\u0442 \u043f\u043e\u0441\u043b\u0435 \u043e\u0442\u0432\u0435\u0442\u0430 (docs/protocol.md 6)",
        font=f_small,
        fill=GRAY,
    )
    pad_x, pad_y = 16, 36
    for row in range(ROWS_VISIBLE):
        text = lines[row] if row < len(lines) else ""
        y = TERM_Y0 + pad_y + row * ROW_H
        color = TERM_GREEN
        if text.startswith("root@"):
            prompt = text[: text.index("#") + 1] if "#" in text else text
            d.text((TERM_X0 + pad_x, y), prompt, font=f_term_b, fill=TERM_PROMPT)
            rest = text[len(prompt):]
            rx = TERM_X0 + pad_x + d.textlength(prompt, font=f_term_b)
            d.text((rx, y), rest, font=f_term, fill=TERM_GREEN)
        elif text.startswith("  "):
            d.text((TERM_X0 + pad_x, y), text, font=f_term, fill=TERM_DIM)
        else:
            d.text((TERM_X0 + pad_x, y), text, font=f_term, fill=color)
        if row == cursor_row and cursor_on:
            cx = TERM_X0 + pad_x + d.textlength(text[:cursor_col], font=f_term)
            d.rectangle([cx, y, cx + 9, y + ROW_H - 3], fill=TERM_GREEN)


def frame(status_text, status_color, top_active, bot_active, top_reveal, bot_reveal,
          term_lines, cursor_row=None, cursor_col=0, cursor_on=True):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_header(d)
    bits_top = "10110100110101101011"
    bits_bot = "11010011011010110100"
    draw_wire(d, WIRE_TOP_Y, "\u043a\u043b\u0438\u0435\u043d\u0442 \u2192 \u0441\u0435\u0440\u0432\u0435\u0440 (CMD / ACK)", top_active, CLIENT_COLOR, bits_top, top_reveal)
    draw_wire(d, WIRE_BOT_Y, "\u0441\u0435\u0440\u0432\u0435\u0440 \u2192 \u043a\u043b\u0438\u0435\u043d\u0442 (SCREEN_FULL / DELTA)", bot_active, AGENT_COLOR, bits_bot, bot_reveal)
    draw_status(d, status_text, status_color)
    draw_terminal(d, term_lines, cursor_row if cursor_row is not None else -1, cursor_col, cursor_on)
    return img


PROMPT = "root@server:~# "

SCREEN_BROKEN = [
    PROMPT + "ip a",
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536",
    "    inet 127.0.0.1/8 scope host lo",
    "2: eth0: <BROADCAST,MULTICAST> mtu 1500",
    "    (\u043d\u0435\u0442 IP \u2014 \u0441\u0435\u0442\u044c \u043d\u0435 \u043f\u043e\u0434\u043d\u044f\u0442\u0430)",
    PROMPT,
]

SCREEN_FIX_RUNNING = [
    PROMPT + "ip a",
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536",
    "    inet 127.0.0.1/8 scope host lo",
    "2: eth0: <BROADCAST,MULTICAST> mtu 1500",
    "    (\u043d\u0435\u0442 IP \u2014 \u0441\u0435\u0442\u044c \u043d\u0435 \u043f\u043e\u0434\u043d\u044f\u0442\u0430)",
    PROMPT + "netplan apply",
]

SCREEN_FIXED = [
    PROMPT + "ip a",
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500",
    "    inet 192.168.1.42/24 scope global eth0",
    "    \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435: UP",
    PROMPT + "ss -tunap | head -3",
    "tcp  LISTEN  0  128  0.0.0.0:22  0.0.0.0:*  sshd",
    PROMPT,
]


def typing_variants(base_lines, row_idx, full_text):
    prefix = base_lines[row_idx]
    out = []
    for n in range(0, len(full_text) + 1, max(1, len(full_text) // 6)):
        lines = list(base_lines)
        lines[row_idx] = prefix + full_text[:n]
        out.append((lines, len(prefix) + n))
    lines = list(base_lines)
    lines[row_idx] = prefix + full_text
    out.append((lines, len(prefix) + len(full_text)))
    return out


frames = []
durations = []


SPEED_FACTOR = 0.5  # 0.5 = в 2 раза медленнее (длительность каждого кадра x2)


def add(img, ms):
    frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=200))
    durations.append(round(ms / SPEED_FACTOR))


def step_exchange(pre_lines, prompt_row, cmd_text, seq, resp_label, post_lines):
    # 1. Печать команды ИИ-агентом на ноутбуке (никакого сигнала ещё нет).
    for lines, n in typing_variants(pre_lines, prompt_row, cmd_text):
        add(frame(f"\u0410\u0418 \u043d\u0430\u0431\u0438\u0440\u0430\u0435\u0442 \u043a\u043e\u043c\u0430\u043d\u0434\u0443\u2026", GRAY, False, False, 0, 0, lines,
                   cursor_row=prompt_row, cursor_col=n, cursor_on=True), 90)
    final_typed = list(pre_lines)
    final_typed[prompt_row] = pre_lines[prompt_row] + cmd_text
    cur_col = len(final_typed[prompt_row])
    # 2. REQ: CMD уходит в эфир по верхнему проводу.
    label = f"REQ  CMD {cmd_text!r}  seq={seq}"
    for reveal in (0.35, 0.7, 1.0):
        add(frame(label, CLIENT_COLOR, True, False, reveal, 0, final_typed,
                   cursor_row=prompt_row, cursor_col=cur_col, cursor_on=True), 110)
    # 3. GAP.
    add(frame("GAP \u2014 \u0442\u0438\u0448\u0438\u043d\u0430, \u0441\u043c\u0435\u043d\u0430 \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f", GRAY, False, False, 0, 0, final_typed,
               cursor_row=prompt_row, cursor_col=cur_col, cursor_on=False), 160)
    # 4. RESP: сервер отвечает снимком экрана по нижнему проводу.
    label2 = f"RESP  {resp_label}  seq={seq}"
    for reveal in (0.35, 0.7, 1.0):
        add(frame(label2, AGENT_COLOR, False, True, 0, reveal, final_typed,
                   cursor_row=prompt_row, cursor_col=cur_col, cursor_on=True), 110)
    # 5. GAP.
    add(frame("GAP", GRAY, False, False, 0, 0, final_typed,
               cursor_row=prompt_row, cursor_col=cur_col, cursor_on=False), 140)
    # 6. ACK по верхнему проводу.
    add(frame(f"ACK  seq={seq}", CLIENT_COLOR, True, False, 1.0, 0, final_typed,
               cursor_row=prompt_row, cursor_col=cur_col, cursor_on=True), 150)
    # 7. Экран клиента обновляется принятым снимком/дельтой.
    last_row = len(post_lines) - 1
    add(frame("\u042d\u043a\u0440\u0430\u043d \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d \u2014 \u0418\u0418 \u0447\u0438\u0442\u0430\u0435\u0442 \u043e\u0442\u0432\u0435\u0442", (140, 220, 150), False, False, 0, 0, post_lines,
               cursor_row=last_row, cursor_col=len(post_lines[last_row]), cursor_on=True), 900)


# --- Сюжет: сеть сломана -> ИИ диагностирует и чинит её по звуку -------------

step_exchange(
    pre_lines=[PROMPT, "", "", "", "", ""],
    prompt_row=0,
    cmd_text="ip a",
    seq=0,
    resp_label="SCREEN_FULL",
    post_lines=SCREEN_BROKEN,
)

step_exchange(
    pre_lines=list(SCREEN_BROKEN[:-1]) + [PROMPT],
    prompt_row=5,
    cmd_text="netplan apply",
    seq=1,
    resp_label="SCREEN_DELTA",
    post_lines=SCREEN_FIX_RUNNING,
)

step_exchange(
    pre_lines=list(SCREEN_FIX_RUNNING[:-1]) + [PROMPT],
    prompt_row=5,
    cmd_text="ip a",
    seq=2,
    resp_label="SCREEN_FULL",
    post_lines=SCREEN_FIXED,
)

# финальная пауза на восстановленном экране
add(frame("\u041a\u0430\u043d\u0430\u043b \u0436\u0438\u0432: \u0441\u0435\u0442\u044c \u043f\u043e\u0447\u0438\u043d\u0435\u043d\u0430 \u0431\u0435\u0437 \u0441\u0435\u0442\u0435\u0432\u043e\u0433\u043e \u0441\u0442\u0435\u043a\u0430", (140, 220, 150), False, False, 0, 0, SCREEN_FIXED,
           cursor_row=len(SCREEN_FIXED) - 1, cursor_col=0, cursor_on=True), 1600)

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_connector_demo.gif")
frames[0].save(
    out_path,
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=False,
)
print("frames:", len(frames), "-> ", out_path)
