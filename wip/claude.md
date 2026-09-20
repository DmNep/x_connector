---
status: idle
worktree: D:/projects/x_connector-claude
branch: wip/claude-idle
task: ""
hands_off: []
updated: 2026-09-20T23:05+03:00
---

Свободен. Починил находку №1 из ревью (AudioTransport._feed() ронял
FrameError демодулятора вместо NAK-пути, docs/protocol.md 8.2) —
xconn_channel/framing.py, xconn_channel/transport.py,
tests/test_transport.py, коммит "Transport: surface corrupted-frame
bytes instead of dropping them". Тесты зелёные (183/183). Остальные 9
находок ревью не тронуты. Каталог Claude: `D:\projects\x_connector-claude`.
Перед задачей заполнить поля и поставить `status: active`.
