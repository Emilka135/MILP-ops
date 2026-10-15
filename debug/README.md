# Проверка и прослушивание

Все новые скрипты прогонов находятся здесь. Исходные unit-тесты остаются в `tests/integration/` и `runtime/tests/`; их журналы, JUnit, WAV, тестовые БД, SCA и SBOM сохраняются в `debug/runs/`, который исключён из Git. Для повторения нужны зависимости и веса из корневого README. Папки результатов создаются автоматически.

| Прогон | Команда | Результаты |
|---|---|---|
| Только тональные клавиши | `python debug/run_dtmf_commands.py` | `runs/dtmf-only/commands.json`, `audio/*.wav` |
| Только настоящая речь, 0 DTMF | `python debug/run_speech_commands.py` | `runs/speech-only/commands.json`, `audio/*.wav` |
| DTMF + речь | `python debug/run_speech_commands.py --mode combined` | `runs/combined/commands.json`, `audio/*.wav` |
| Скорости 1,3 / 1,5 / 1,75, числа, даты, время, G.711 | `python debug/run_speech_roundtrip.py` | `runs/speech-only/recognition-quality.json` |
| ИИ-автоответчик / чужой человек | `python debug/run_unverified_recipient.py --orchestrator http://127.0.0.1:8080 --tts-url http://127.0.0.1:8080` | `runs/answering-machine/` |
| Оператор недоступен / экстренный сигнал | `python debug/run_callback.py` | `runs/callback/` |
| Сравнение двух NLU | `python debug/run_nlu_benchmark.py` | `runs/nlu/benchmark.json`, 5 повторов |
| Реальный банк постоянных фраз | `TTS_MODEL=modules/tts/models/silero-v5_5_ru.pt python debug/run_audio_bank.py` | `runs/audio-bank/result.json`, два слышимых WAV |
| Браузер конструктора и аудиобанк | `TEST_AUDIO_BANK=1 node debug/run_editor_browser.mjs` | `runs/editor-browser/`, 6 скриншотов |
| Все автоматические проверки | `python debug/run_checks.py` | `runs/checks/`, `runs/integration/`, `runs/legacy/` |
| Фактический ARI / AudioSocket | `ARI_PASSWORD=… python debug/run_asterisk_contract.py` | `runs/containers/asterisk-contract.json`; PBX должен работать |
| Сохранённые MILP-проверки | `python debug/run_legacy_milp.py` | вывод / runner `run_checks.py` |
| Слышимые образцы интерфейса | `python debug/export_audio.py` | `runs/speech-only/audio/` |

Перед первыми тремя командами запустите `python tools/dev.py` или Compose. `--orchestrator` меняет адрес; `--tts-url` нужен только для обращения непосредственно к отдельному TTS-сервису. В контейнерах TTS доступен через защищённый API оркестратора. Для callback запустите отдельный стенд: `OPERATOR_AVAILABLE=0 CRM_NOW=2026-10-15T23:00:00+04:00 python tools/dev.py --base-port 28280`.

Тональный прогон синтезирует настоящие пары частот всех 16 клавиш, передаёт PCM20ms в production `Bridge.audio`, детектор и арбитр, затем выполняет команды на Rust. Проверяются PCM / A-law / μ-law. ASR не вызывается; речь в нём не подменяет тоны. Речевой прогон отправляет только PCM настоящего Silero в ASR и не вводит ни одной цифры DTMF. Каждый прогон независимо создаёт, подтверждает, переносит и отменяет собственную тестовую запись. Отсутствующие модели, неслышимый TTS и неверный результат завершают речевую проверку ошибкой.

Откройте [listen.html](listen.html), чтобы прослушать сохранённое приветствие, команды и сами тоны. Клавиши на этой странице воспроизводят WAV; операции с МИС проверяют скрипты, результаты которых перечислены выше. Новые образцы можно получить `export_audio.py`; коммерческие условия весов перечислены в `docs/LICENSES.md`.

В прогонах используются только синтетические пациенты. Не направляйте их на МИС организаторов или внешний SIP-транк. CI сохраняет `debug/runs/` отдельным артефактом, включая WAV и JUnit.
