# Голосовой ассистент регистратуры

Объединение MILP-ops и calls-arena: Rust исполняет сценарии, ведёт звонки и отдельные ограниченные очереди модулей; Python обслуживает локальные модели, NLU, аудио и SQLite-зеркало МИС. Запись, перенос и отмена завершаются только после подтверждения МИС. Потерянный ответ блокирует спорный слот и создаёт заявку оператору.

По комментариям заказчика сначала используется DTMF, после 60 секунд без нажатий включается ASR; если человек говорил, обращение передаётся оператору. Внутри переноса доступны речевые даты, время и «да/нет». Звёздочка вызывает оператора на любом шаге. Боль, жалобы, медицинские вопросы и конкретный врач передаются человеку. Наличие и давность направления уточняются отдельно.

## Запуск главного контейнера

Главный сервис — **`orchestrator`** (Rust). Команда ниже запускает его и отдельные контейнеры зависимостей из `depends_on`, ждёт их healthcheck и готовности оркестратора. Во время звонка Rust обращается к модулям по HTTP во внутренней сети Compose; запуск контейнеров выполняет Compose.

```bash
cp .env.example .env
python3 tools/check_space.py --write-path modules --expected-gib 2
# Один раз: в Python-окружении с requirements/asr.txt и huggingface_hub
python3 tools/download_models.py --tts --asr
# Главный контейнер вместе с необходимыми модулями:
docker compose up --build -d --wait orchestrator
# Проверка состояния и готовности:
docker compose ps
curl --fail http://127.0.0.1:8080/readyz
docker compose logs --tail=100 orchestrator crm tts asr
```

API: `http://127.0.0.1:8080`; окно регистратора: `http://127.0.0.1:8080/operator`. При изменении `HTTP_PORT` используйте его в URL. В live API требует `Authorization: Bearer …`; `/readyz` остаётся технической проверкой. Настройка SIP ниже запускается отдельным профилем. Завершение: `docker compose down`; данные в именованных томах сохраняются.

Каждый вычислительный модуль имеет собственный контейнер и очередь Rust на **128 запросов с 4 рабочими задачами**. У каждого запроса свои `request_id`, ответ и дедлайн, включающий ожидание в очереди; переполнение отклоняется. Звонки обслуживаются параллельно, состояние пациента изолировано. Лимит 1000 активных сессий — защитный предел, а не доказательство обработки 1000 одновременных разговоров. Схема: [ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Пропускная способность:** исходное ТЗ задаёт до 200 звонков в пиковый день, средний разговор 4–5 минут и среднее ожидание до 30 секунд. Лимит стенда — **10 HTTP-запросов в минуту на команду**, поэтому за 10 минут доступно до 100 запросов, включая синхронизацию. Это максимум 100 изменений существующих записей или 50 новых записей, если на каждую нужны создание пациента и визита; фактический результат ниже при прочих запросах. Локальное чтение расписания и предзаписанные фразы уменьшают обращения к дорогим модулям. Расчёт для 12/16/24/32 GiB, формула с ASR/TTS и границы замеров: [CAPACITY.md](docs/CAPACITY.md). Целевые показатели ТЗ нельзя считать достигнутыми по одному healthcheck.

На машине должно оставаться строго **больше 10 GiB на `/home` и 5 GiB на `/`**, в том числе после ожидаемой записи. Проверка: `python3 tools/check_space.py --write-path target --expected-gib 1`. Тяжёлые сборки и тома при необходимости размещайте на своём разделе `/mnt/docker_space`; не меняйте внутренние файлы Docker вручную и не очищайте чужие ресурсы.

## Локальная разработка и реальные аудиопроверки

Нужны Rust 1.98, Python 3.12+ и локальные веса. Все речевые проверки используют настоящий Silero и ASR; демо отличается синтетическими данными МИС и имитацией доставки СМС.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements/dev.txt
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
pip install -r requirements/asr.txt omegaconf==2.3.0
python tools/download_models.py --tts --asr
python tools/dev.py --smoke
python tools/dev.py
```

Логи и тестовая БД: `debug/runs/local-stack/`. Ctrl-C останавливает дочерние процессы. Модель TTS — `modules/tts/models/silero-v5_5_ru.pt`. Веса исключены из Git; условия использования: [LICENSES.md](docs/LICENSES.md).

```bash
python debug/run_dtmf_commands.py
python debug/run_speech_commands.py
python debug/run_speech_commands.py --mode combined
python debug/run_speech_roundtrip.py
```

Все прогоны и их результаты находятся в [debug/](debug/README.md). `dtmf-only` передаёт реальные звуковые тоны через production-детектор и арбитр без ASR. `speech-only` передаёт только PCM настоящей речи, без DTMF и подстановки текста. `combined` проверяет оба пути. В `debug/runs/*/audio/` сохраняются WAV для прослушивания. Расширенная проверка речи охватывает скорости 1,3 / 1,5 / 1,75 и G.711.

Стандартный входящий сценарий — DTMF первым. Отдельный явно выбранный `flow=voice` позволяет говорить с первого шага. Исходящий звонок сначала проверяет номер карты; ответ автоответчика или чужого человека не подтверждает запись. Если оператор недоступен, неэкстренное обращение запрашивает удобное время обратного звонка и сохраняется в БД. Экстренные сигналы используют отдельный маршрут.

## Стенд и SIP

Скопируйте `.env.example` в `.env`. Для подключения стенда задайте `APP_MODE=live`, `WITH_ML=1`, `API_TOKEN`, `STAND_URL=https://crm-med-mock.dbi-testing.ru/api/v1`, `STAND_TOKEN`, импорты расписания/пациентов и СМС-шлюз. Затем `docker compose up --build -d --wait`. В live отсутствующие модели и токен останавливают соответствующий сервис. API принимает `Authorization: Bearer …`.

Нужен экспорт пациентов и настоящее расписание с **явными doctor_id стенда**. В `data/schedule_grid.json` сохранены демонстрационные допущения исходника, это не подтверждённое расписание организаторов. Контракт: [STAND.md](docs/STAND.md).

Для Asterisk задайте разные длинные `ARI_PASSWORD`, `SIP_PHONE_PASSWORD`, `SIP_OPERATOR_PASSWORD` и выполните `docker compose --profile telephony up --build -d`. Зарегистрируйте SIP-клиент `phone` на порту 5060 и позвоните на **100**. Второй клиент `operator` получает переводы. Настройка транка и сети: [TELEPHONY.md](docs/TELEPHONY.md).

## Проверка

Назначение каждого модуля, условия прохождения и порядок отдельных подписанных коммитов: [план приёмки](docs/acceptance/GOAL.md).

```bash
cargo fmt --all --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked --bin clinic-voice
python -m pytest tests/integration
python debug/run_legacy_milp.py
(cd runtime && PYTHONPATH=src python -m pytest)
cargo run --release --locked --bin bench-nlu
```

GitHub Actions: тесты, SCA, лицензии Rust, функциональная проверка контейнеров, CycloneDX SBOM. Настоящие речевые команды обязательны в основных тестах и контейнерной приёмке; расширенный замер доступен через workflow `real-local-speech`. Результаты и границы проверки: [VALIDATION.md](docs/VALIDATION.md).

| Путь | Назначение |
|---|---|
| `src/` | Rust VM, HTTP, очереди, корреляция, два классификатора и SQL-планы |
| `scenarios/*.flow` | редактируемые графы, при сборке превращаются в Rust `match` |
| `runtime/src/clinic/` | объединённые адаптеры и настоящие `/clients` + `/visits` |
| `runtime/src/calls/` | DTMF, нормализатор, TTS/ASR и другие модули calls-arena |
| `app/` | сохранённый MILP-ops; используются safety, HTTP-контракты и точный mock МИС |
| `docs/upstream/` | исходные README, лицензия calls-arena, SHA256 обоих архивов |
| `docs/requirements/` | исходное ТЗ и комментарии |

Документы: [архитектура](docs/ARCHITECTURE.md), [сборка](docs/BUILD_REQUIREMENTS.md), [эксплуатация](docs/RUNTIME_REQUIREMENTS.md), [ASR / 2026](docs/RESEARCH_ASR_2026.md), [Крылов / Deeray](docs/RESEARCH_KRYLOV.md), [мощность 12/16/24/32 GiB](docs/CAPACITY.md), [соответствие ТЗ](docs/TRACEABILITY.md).
