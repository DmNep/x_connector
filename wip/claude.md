---
status: idle
worktree: D:/projects/x_connector-claude
branch: wip/claude-idle
task: ""
hands_off: []
updated: 2026-09-21T00:35+03:00
---

Свободен. Починил находку №6 из ревью (AudioTransport.send() слал
T_LEAD перед каждым кадром вместо одного раза за обмен перед ответом,
docs/protocol.md 8.1, 8.4) — теперь T_LEAD только перед ответом на
только что принятый кадр (флаг _pending_lead). Заодно вскрылась и
починена смежная хрупкость теста test_idle_reset_recovers_next_frame,
которая случайно держалась на старом безусловном T_LEAD.
xconn_channel/transport.py, tests/test_transport.py, коммит
"Transport: only send T_LEAD before a reply, not before every frame".
Тесты зелёные (189/189). Находки №1–5 починены ранее в этой сессии.
Остались 7–10. Каталог Claude: `D:\projects\x_connector-claude`. Перед
задачей заполнить поля и поставить `status: active`.
