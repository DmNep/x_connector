---
status: idle
worktree: D:/projects/x_connector-claude
branch: wip/claude-idle
task: ""
hands_off: []
updated: 2026-09-21T00:15+03:00
---

Свободен. Починил находки 1-4 своего же ревью tools/ (кроме кода
канала, чужого не трогал):
1. SNR всегда inf на --live (tools/ber.py) — snr_db теперь берёт
   измеренный noise_floor, а не всегда-нулевой noise_sigma.
2. find_onset не отличал «сигнала нет» от «фронт на 0» (tools/live.py,
   tools/probe.py) — теперь None вместо 0, slice_live_capture падает
   явно.
3. --frames 0/отрицательное давал ложный PASS (tools/ber.py) —
   argparse-валидатор отклоняет некорректное значение. Коммит попал в
   один пуш с находкой №1 (оба правили ber.py) — код и тесты внутри
   того же коммита "ber.py: SNR uses measured noise...", просто не
   вынесено в отдельный.
4. AlsaAudio.__init__ мог осиротить aplay, если arecord не поднимался
   (xconn_channel/audioio.py) — теперь terminate()/wait() на aplay
   перед re-raise.
Тесты зелёные (231/231). Находки 5-10 (dead-tone imbalance, тихий
фолбэк устройства, отсутствие wav-бэкенда в live.py, непротестированный
CLI-вход трёх утилит, дублирование _write_report и Гёрцеля) не трогал.
Каталог Claude: `D:\projects\x_connector-claude`. Перед задачей
заполнить поля и поставить `status: active`.
