# calls — голосовой помощник регистратуры поликлиники

Хакатон «Точка сборки», кейс Cloud.ru / интегратора: поликлиника на 85–100 врачей, 12 регистраторов, 120 000 пациентов,
1 300 визитов в день, 30–40 % звонков теряются в пик. Решение принимает входящие звонки (запись к врачу, своя запись,
справка), обзванивает пациентов за день до приёма (подтвердить / отменить / перенести), переводит на оператора всё,
что робот не должен решать сам (жалобы, острая боль, медицинские вопросы, конкретный врач по фамилии), и пишет
результат в CRM-стенд через API. Приоритет №1 — **ноль ошибочных записей**, №2 — доля звонков без оператора.

* Телефония: Asterisk (ARI + AudioSocket), G.711, DTMF по RFC 4733 **и** собственный in-band детектор (ITU-T Q.24, 72 000-ячеечная матрица).
* Политика диалога: **сначала DTMF**; 60 с без нажатий → ASR; речь, которую нельзя безопасно исполнить → оператор с полным контекстом.
* Идентификация без персональных данных в подсказках: «вы или другой человек?» → дата рождения / СНИЛС / номер карты (цифрами); телефон запрашивается, если не совпал с базой.
* Каждый звонок заканчивается голосовым статусом заявки (Silero, ускорение 1,3–1,75×) и СМС с именем записанного человека.
* Два NLU-подхода реализованы и сравнены по скорости: классификатор → switch-case (+ второй агент-верификатор) и детерминированный автомат текст → SQL.
* Каждый модуль — свой контейнер; SCA/SBOM/тесты/проверка запускаемости контейнеров — в GitHub Actions.

## Вход и выход (по ТЗ кейса)

Критерий успеха автопрогонов — **правильная запись в стенде через API** (приём / перенос / отмена / подтверждение,
перевод на оператора = тоже запрос в API). Поэтому контракт «вход → выход» зафиксирован в коде, проверяется тестами и
прогоняется на открытых тест-кейсах в CI. Подробно — [docs/STAND.md](docs/STAND.md).

| | Что | Откуда / куда | Где в коде |
|---|---|---|---|
| **Вход 1** | звонок: входящий SIP (DTMF по RFC 4733 + in-band, речь → ASR) или исходящее напоминание | Asterisk ARI/AudioSocket → оркестратор (`call.start/dtmf/speech/partial/silence`) | `calls.telephony`, `calls.orchestrator.app` |
| **Вход 2** | расписание врачей — Excel организаторов (`.xlsx`/`.csv`) | `CRM_SCHEDULE` → локальное зеркало SQLite (стенд читать нельзя: 10 RPM) | `MockRegistry.load_schedule_xlsx/csv` |
| **Вход 3** | пациенты (СНИЛС, карта, дата рождения, телефон) | `CRM_PATIENTS` (csv/xlsx) или демо-данные `CRM_SEED_DEMO=1` | `load_patients_csv/xlsx` |
| **Вход 4** | записи, уже существующие в стенде (для напоминаний, отмены, переноса) — с **id стенда** | `CRM_BOOKINGS` (csv/json/xlsx) или `StandSync.pull_bookings(day)` — 1 GET на день | `import_booking`, `calls.crm.stand` |
| **Вход 5** | OpenAPI стенда + токен команды | `STAND_URL`, `STAND_TOKEN`, `STAND_ENDPOINTS` = `config/stand.endpoints.json` (пути и тела запросов правятся в одном JSON) | `calls.crm.remote.EndpointMap` |
| **Вход 6** | открытые тест-кейсы | `cases/*.json`: шаги звонящего → ожидаемые записи в стенде | `tools/run_cases.py`, `calls.cases.runner` |
| **Выход 1** | **записи в стенде**: `book` → `POST /appointments`, `reschedule` → `PATCH /{id}`, `cancel` → `DELETE /{id}`, `confirm` → `POST /{id}/confirm`, `transfer` → `POST /operator-requests` | через `Outbox` (FIFO, ≤ 10 RPM, 429 → ждём `Retry-After`, 3 попытки → заявка оператору `stand_sync_failed`) | `calls.crm.stand.StandSync`, `calls.crm.remote` |
| **Выход 2** | голосовой статус заявки в конце разговора (Silero) и СМС с именем записанного | `say`/`sms` в сценарии | `scenarios/*.flow`, `calls.notify.sms` |
| **Выход 3** | заявка оператору с полным контекстом (SQL-подсветка подходящих слотов, состояние звонящего) | `operator` → стенд + консоль HTML | `calls.crm.operator` |
| **Выход 4** | журнал всех записей в стенд (`STAND_JOURNAL` jsonl), `POST /v1/stand.status` (pending/done/failed, по зеркалу), `/metrics` | crm / operator / orchestrator | `calls.services` |
| **Выход 5** | отчёты CI: таблица «вход → выход» по кейсам (`reports/cases.md`, в summary джоба), Q.24-матрица, bench NLU, capacity | GitHub Actions | `.github/workflows/ci.yml`, `containers.yml` |

```bash
python tools/run_cases.py                       # 24 кейса: in-process оркестратор + фейковый стенд → таблица вход/выход
docker compose up -d && python tools/run_cases.py --orchestrator http://localhost:8080 \
    --stand http://localhost:8087 --crm http://localhost:8081   # то же против контейнеров (crm/operator пишут в стенд)
curl -s -H 'authorization: Bearer team-token' localhost:8087/api/v1/appointments   # что реально оказалось в стенде
```

## Документация

| Документ | Содержание |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | архитектура L0–L2, контракты, бюджет задержек, медиаплоскость |
| [docs/SCENARIO_DSL.md](docs/SCENARIO_DSL.md) | формат графа сценария `.flow`, валидатор, компиляция в switch-case и Mermaid |
| [docs/RESEARCH_ASR_2026.md](docs/RESEARCH_ASR_2026.md) | исследование потокового русского ASR 2026 (GigaAM v3, Deeray runtime, T-one, Vosk, Whisper), рейтинг по WER, числа/даты, что с «Крыловым» |
| [docs/RESEARCH.md](docs/RESEARCH.md) | факты по DTMF (Q.23/Q.24, RFC 4733), ARI, Silero VAD, протоколу Deeray |
| [docs/LICENSES.md](docs/LICENSES.md) | таблица разрешённых лицензий и инвентаризация компонентов (проект MIT; TTS — Silero `v5_cis_base`, MIT) |
| [docs/STAND.md](docs/STAND.md) | **вход и выход по ТЗ**: что решение читает (расписание Excel, пациенты, записи стенда, звонок) и что пишет в стенд через API (book / reschedule / cancel / confirm / transfer, ≤ 10 RPM, журнал), формат тест-кейсов |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | требования к сборке и запуску: Python, extras, контейнеры, телефония, стенд, CI |
| [docs/CAPACITY.md](docs/CAPACITY.md) | расчёт звонков за 10 минут для GPU 12 / 16 / 24 / 32 GB (генерируется `tools/capacity.py`) |
| [docs/RUST_PORT.md](docs/RUST_PORT.md) | почему Python, замеры, план переноса горячего пути на Rust |
| [docs/flows/](docs/flows/) | Mermaid-диаграммы сценариев (генерируются из `.flow`) |

## Быстрый старт

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m calls.scenario.compile scenarios --check     # граф → switch-case + Mermaid, проверка свежести
pytest                                                 # 390 тестов (в т.ч. 24 открытых кейса), ~30 с

# один модуль как HTTP-сервис (stdlib, без зависимостей)
python -m calls.services orchestrator --port 8080      # + crm | nlu | tts | sms | operator | asr | stand
# запись в стенд: STAND_URL=http://localhost:8087 STAND_TOKEN=team-token python -m calls.services orchestrator
curl -s localhost:8080/health
curl -s -X POST localhost:8080/v1/call.start -H 'content-type: application/json' \
     -d '{"call_id":"demo","flow":"inbound","caller_phone":"79035551234"}'
curl -s -X POST localhost:8080/v1/call.dtmf -H 'content-type: application/json' -d '{"call_id":"demo","digit":"1"}'
curl -s -X POST localhost:8080/v1/call.state -H 'content-type: application/json' -d '{"call_id":"demo"}'
```

Сценарный прогон без телефонии (то, чем пользуются тесты-«гриндеры»):

```python
from datetime import date
from calls.crm.module import CrmModule
from calls.crm.registry import MockRegistry, seed_demo
from calls.orchestrator.simulate import run_call
from calls.scenario.dsl import load_flows

today = date(2026, 10, 14)
reg = MockRegistry(); seed_demo(reg, today)
flows = load_flows("scenarios")
tr = run_call(flows, "inbound", CrmModule(reg, today=lambda: today),
              ["1", "1", ("digits", "12051980"), "1", "1"],          # запись → себя → дата рождения → терапевт → да
              vars={"caller_phone": "79035551234", "today": today.isoformat()}, today=today)
print(tr.status, tr.sms[0]["vars"]["patient_name"], tr.prompts[-1])  # booked Иван Иванович «Повторяю: …»
```

## Контейнеры

```bash
docker compose build base && docker compose build              # базовый образ, затем модули
docker compose up -d                                           # crm, nlu, sms, operator, orchestrator, outbound (без ML, ~60 MB RSS каждый)
docker compose --profile ml up -d tts asr                       # Silero v5_cis_base (MIT; положите modules/tts/models/v5_cis_base.pt) + onnx-asr GigaAM v3
docker compose --profile telephony up -d telephony ari-bridge   # Asterisk + мост ARI/AudioSocket
```

Оркестратор находит модули по `CRM_URL`, `NLU_URL`, `TTS_URL`, `SMS_URL`, `OPERATOR_URL`, `ASR_URL`; без URL модуль работает
in-process. Без моделей сервисы стартуют со стабами (TTS — тишина нужной длины, ASR — эхо текста) — пайплайн и тесты от них не зависят.
Переменные стенда: `STAND_URL`, `STAND_TOKEN` (лимит 10 RPM → `TokenBucket`, запись через `Outbox`), `CRM_SCHEDULE=/data/schedule.xlsx`.

## Структура

```
scenarios/            inbound.flow, reschedule.flow, reminder.flow — графы диалогов (источник истины)
src/calls/
  audio/ dtmf/ asr/   G.711, детектор DTMF (Гёрцель Q.24), арбитр OOB/in-band, сборщик цифр, модель событий ASR, трекер протокола Deeray
  nlu/                normalize (числа/даты/время/да-нет, автокоррекция), intent (правила), embed (хеш / USER-bge-m3),
                      agent (два агента: классификатор + верификатор), text2sql (детерминированный автомат → SQL)
  scenario/           dsl (парсер + валидатор), engine (исполнение), compile (→ Python switch-case + Mermaid), compiled/
  crm/                registry (SQLite-зеркало, импорт Excel/CSV: расписание, пациенты, записи стенда), module (операции для сценария),
                      operator (тикет с подсветкой слотов SQL), remote (TokenBucket 10 RPM, Outbox с Retry-After, EndpointMap из JSON),
                      stand (StandSync: записи зеркала → API стенда, учёт id стенда, отказ → заявка оператору), fake_stand (стенд-заглушка)
  cases/              раннер открытых тест-кейсов: вход (шаги звонящего) → выход (записи в стенде), локально и против контейнеров
  orchestrator/       bus (EDF-очереди на модуль, метрики), session (актор звонка, таймеры), modules, app (HTTP-фасад), simulate
  tts/                verbalize (числа/даты словами), stress (ударения для v5_cis_base: словарь всего словаря подсказок + silero-stress для фамилий), silero (SSML fast + WSOLA до 1,75×, LRU-кэш, стаб)
  nlu/early.py        раннее намерение + состояние абонента (< 3 с) по partial-ам потокового ASR и просодии — собственная конструкция (SPRT-подобная остановка), эскалация боли/жалобы до конца фразы, состояние в тикете оператора
  notify/ outbound/   СМС-шаблоны с именем записанного; политика обзвона 10:00–22:00, ≤ 6 вызовов/24 ч, кампания за день
  services/           HTTP-обёртка модуля (stdlib), точка входа `python -m calls.services <module>`
  telephony/          ari_bridge — ARI WebSocket + AudioSocket + арбитр DTMF ↔ оркестратор
tools/                bench_nlu (switch-case vs text→SQL), capacity, cluster_calls (записи → кластеры → заготовка .flow),
                      e2e_tts_asr (текст → TTS → канал → ASR → нормализация), dtmf_matrix
tests/                390 тестов: DTMF-матрицы, нормализатор, DSL, e2e-гриндер с имитацией ошибок ASR, шина, потоки,
                      удалённая топология (все модули по HTTP), ARI-мост, политика обзвона, СМС, TTS, вход/выход стенда, 24 открытых кейса
cases/ config/ data/samples/   тест-кейсы (inbound.json, reminder.json), маппинг на OpenAPI стенда, образцы входных файлов
docker/ docker-compose.yml telephony/asterisk/   образы модулей, компоновка, минимальный dialplan/ARI
.github/workflows/    ci (тесты+отчёты), sca (pip-audit, bandit, ruff, лицензии), sbom (CycloneDX+grype), containers (запускаемость)
```

## Инструменты и отчёты

```bash
python tools/run_cases.py --md reports/cases.md    # открытые кейсы: вход → выход; --orchestrator/--stand/--crm — против контейнеров
python tools/bench_nlu.py --n 2000                 # скорость/точность: правила, два агента, text→SQL (+ выполнение на зеркале)
python tools/e2e_tts_asr.py                        # 71 кейс чисел/дат/времени через симулятор ASR; --tts silero --asr onnx|deeray|vosk — реальные модели
python tools/capacity.py --md docs/CAPACITY.md     # звонков/10 мин для 12/16/24/32 GB
python tools/cluster_calls.py recordings/ --asr onnx   # записи → транскрипты → кластеры → reports/clusters.{md,flow}
python tools/dtmf_matrix.py --quick                # матрица приёмки Q.24
python tools/stress_vocab.py --check               # словарь ударений покрывает весь словарь подсказок (для MIT-модели v5_cis_base); --schedule расписание.xlsx добавит фамилии врачей
python tools/check_licenses.py --md reports/licenses.md   # allow-list лицензий зависимостей (понимает SPDX AND/OR)
```

Результаты на 2 vCPU: правила → intent 180 µs (p50), text→SQL 125 µs, два агента 500 µs; точность на синтетическом корпусе
100 % / 92 % / 100 % (чистый текст) и 94 % / 88 % / 97 % (50 % искажений ASR). Гриндер: **0 ошибочных записей** при 0 / 50 / 100 %
зашумлении, самообслуживание ≥ 97 % на чистом и ≥ 80 % на шумном тексте.

## Статус

Реализовано и покрыто тестами: DTMF-тракт (S3), нормализатор и оба NLU, DSL сценариев + компилятор, три сценария
(входящий, перенос, напоминание), CRM-зеркало с операциями, тикет оператора с SQL-подсветкой слотов, оркестратор с очередями
и per-call акторами, СМС, политика обзвона, HTTP-сервисы, ARI-мост, контейнеры и CI. Требует подключения на стенде:
реальные эндпоинты CRM (`EndpointMap`), SIP-транк, файл `v5_cis_base.pt` (MIT; `v5_5_ru.pt` — только с лицензией Silero), веса GigaAM v3 / образ Deeray.
