---
status: idle
worktree: D:/projects/x_connector-claude
branch: wip/claude-idle
task: ""
hands_off: []
updated: 2026-09-25T05:40+03:00
---

Свободен. Добил хвост своего же ревью tools/ (находки 5-10, часть уже
была закрыта параллельно Cursor'ом — wav-бэкенд, dedup _write_report,
внятные ошибки устройств):
5. Мёртвый опорный тон 1200 Гц отчитывался как «пара пригодна»
   (tools/probe.py, tone_imbalance/format_report) — теперь отдельный
   флаг reference_dead вместо db_ratio(+inf).
8. main()/build_parser() трёх утилит не гонялись тестами целиком —
   добавил TestMainCli в test_probe.py и test_ber.py (argv → код
   возврата → текст отчёта).
10. Дублирование Goertzel/generate_tone с demodulator.py/modulator.py
    — оставил как есть: разные по форме алгоритмы (блочный офлайн-анализ
    vs потоковый PLL), Cursor уже задокументировал разницу в докстринге.
Тесты зелёные (287/287). Код xconn_channel/ не трогал. Каталог Claude:
`D:\projects\x_connector-claude`. Перед задачей заполнить поля и
поставить `status: active`.
