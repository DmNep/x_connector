---
status: idle
worktree: D:/projects/x_connector-claude
branch: wip/claude-idle
task: ""
hands_off: []
updated: 2026-09-21T01:35+03:00
---

Свободен. Починил находку №10, последнюю из ревью (SlideGoertzel,
EnergyGate, PreambleTracker в demodulator.py держали скользящее окно
на list с pop(0)/del list[:k] — O(window) на каждый отсчёт на hot
path) — перевёл все три на collections.deque, O(1) на отсчёт.
xconn_channel/demodulator.py, коммит "Demodulator: back sliding
windows with deque instead of list". Поведение не менялось — только
структура данных; существующие TestEnergyGate/TestPreambleTracker/
TestSlideGoertzel в test_modem.py уже гоняют данные далеко за одно
окно, полный прогон зелёный (194/194). Все 10 находок ревью из этой
сессии починены. Каталог Claude: `D:\projects\x_connector-claude`.
Перед задачей заполнить поля и поставить `status: active`.
