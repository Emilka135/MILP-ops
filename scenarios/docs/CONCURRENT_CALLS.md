# Проверка двадцати одновременных звонков

Профиль проверяет реальные WAV → локальный ASR → E5 → Rust FSM → Silero TTS → архив. Двадцать сессий стартуют общим barrier, затем каждая выполняет две реплики с barrier перед каждой. Отдельно выполняются речь, DTMF и смешанный ввод. Это короткий путь к `callback.ask_consent`, без оформления визита; результат нельзя переносить на двадцать одновременных транзакций внешней МИС с лимитом 10 RPM.

Время начинается при отправке уже законченной пользовательской PCM-реплики и заканчивается после получения всего ответного PCM. Речь пользователя и воспроизведение ответа в реальном времени не включены. Лимит каждой реплики строго меньше 60 секунд; общий deadline процесса 240 секунд. Endpoint/VAD и фоновые шумы нужны в отдельной проверке телефонии: этот профиль принимает законченную реплику.

Runner проверяет неизменность WAV, четыре исходных голоса и темп 0.75–2.5; фактические ASR-реплики, call/registrar correlation, trace и независимый целевой state. Ревизия архива должна совпасть с заново скомпилированным текущим `.flow`. Проба ASR требует настоящий `onnx-asr` и CPU/CUDA provider; для CUDA обязательно `cuda_verified=true`. Перед и после нагрузочного прогона bookings совпадают, журнал транзакций и SMS пусты. Полные архивы сохраняются рядом с ответными WAV.

## Локальный GPU-профиль

Требуются существующие проектные образы и модели в `modules/`. Проверить `python tools/check_space.py`: после операции больше 5 GiB на `/home` и `/`. Отдельный compose не соединять с production compose: он не монтирует секреты или живую БД, использует закрытую Docker сеть и временные CRM/audio.

```bash
export ACCEPTANCE_MODELS_ROOT="$PWD/modules"
docker compose -f compose.acceptance.yaml up -d
mkdir -p debug/runs/concurrent-calls
docker run --rm --network clinic-concurrent-acceptance_isolated \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/source:ro" -v "$PWD/debug/runs/concurrent-calls:/evidence" \
  -w /source clinic-voice-crm python debug/run_concurrent_calls.py \
  --base http://orchestrator:8080 --crm http://crm:8080 \
  --nlu http://nlu:8080 --tts http://tts:8080 --asr http://asr:8080 \
  --mode speech --output /evidence/speech
```

Закрытая сеть может не публиковать localhost-порты в текущем Docker. Runner запускается внутри неё, с адресами сервисов. `dtmf` и `mixed` запускать отдельно тем же способом. Завершение только этого стенда: `docker compose -f compose.acceptance.yaml down`; компиляторный cache volume сохраняется, временная CRM удаляется при остановке.

## Actions и act

Workflow `.github/workflows/concurrent-audio.yml` по умолчанию поднимает отдельные CPU-сервисы с локальными моделями и новой SQLite. Не регенерирует эталонные WAV. Подготовка моделей допускает download публичных весов; звонки не обращаются к внешним LLM/МИС.

Для act с уже подготовленным локальным GPU-стендом:

```bash
act push -W .github/workflows/concurrent-audio.yml -j audio \
  -P ubuntu-latest=catthehacker/ubuntu:act-24.04 --pull=false \
  --action-offline-mode --bind --network clinic-concurrent-acceptance_isolated \
  --env CLINIC_AUDIO_EXTERNAL_STACK=1 \
  --env CLINIC_AUDIO_BASE=http://orchestrator:8080 --env CLINIC_AUDIO_CRM=http://crm:8080 \
  --env CLINIC_AUDIO_NLU=http://nlu:8080 --env CLINIC_AUDIO_TTS=http://tts:8080 \
  --env CLINIC_AUDIO_ASR=http://asr:8080 \
  --env CLINIC_AUDIO_OUTPUT_DIR=debug/runs/concurrent-calls/act
```

`EXTERNAL_STACK` означает подготовленные тестовые сервисы. Проверки источника CRM, графа и настоящего ASR остаются обязательными. Это не доказательство прохождения отдельной подготовки CPU-зависимостей на GitHub hosted runner.

## Полученный результат 22.10.2026

Последний act: по 20 сессий × 2 реплики, все целевые состояния совпали. Максимум: speech 1.297с, DTMF 0.182с, mixed 1.270с. CPU Silero с context `silero-stress`; настоящий CUDA GigaAM, выполненные CUDA kernels подтверждены. Отчёты: `debug/runs/concurrent-calls/act/*/report.json`; лог `debug/runs/score-act/real-twenty-audio-artifacts.log`.

Первый прогон намеренно сохранён как failed: safety-основа `перекос` обрезалась до `перек`, поэтому «Переключите меня на оператора» ошибочно объявлялась экстренным обращением. Уточнён именно этот stem; шесть новых проверок и 58 прежних safety-тестов подтверждают обычный перевод и сохранение приоритета реальных тревожных фраз. WAV и ожидаемая цель не менялись.

Один полный настоящий архив из этого прогона импортирован в реальный HTTP/SQLite кабинет регистратора: Chromium сверил все реплики, trace и последний state, затем admin restore и scoped registrar proposal. Семь проверок прошли; screenshot просмотрен. `client/result.json` не заявляет повторное выполнение ASR/FSM браузером: происхождение обеспечивает связанный нагрузочный прогон и сохранённый `client-archive.json`.
