# Голосовой ассистент регистратуры

Объединение MILP-ops и calls-arena: Rust исполняет сценарии, ведёт звонки и отдельные ограниченные очереди модулей; Python обслуживает локальные модели, NLU, аудио и SQLite-зеркало МИС. Запись, перенос и отмена завершаются только после подтверждения МИС. Потерянный ответ блокирует спорный слот и создаёт заявку оператору.

По комментариям заказчика сначала используется DTMF, после 60 секунд без нажатий включается ASR; если человек говорил, обращение передаётся оператору. Внутри переноса доступны речевые даты, время и «да/нет». Звёздочка вызывает оператора на любом шаге. Боль, жалобы, медицинские вопросы и конкретный врач передаются человеку. Наличие и давность направления уточняются отдельно.

## Быстрый запуск

Нужны Rust 1.98, Python 3.12+ и локальные веса. Все режимы используют настоящий Silero и ASR; демо отличается синтетическими данными МИС и имитацией доставки СМС.

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

API: `http://127.0.0.1:8080`; окно регистратора: `http://127.0.0.1:8080/operator`. Логи и тестовая БД: `debug/runs/local-stack/`. Ctrl-C останавливает дочерние процессы. Нужная модель TTS — `modules/tts/models/silero-v5_5_ru.pt`. Веса исключены из Git; условия использования: [LICENSES.md](docs/LICENSES.md).

```bash
docker compose up --build -d --wait
python debug/run_dtmf_commands.py
python debug/run_speech_commands.py
python debug/run_speech_commands.py --mode combined
python debug/run_speech_roundtrip.py
docker compose down
```

Все прогоны и их результаты находятся в [debug/](debug/README.md). `dtmf-only` передаёт реальные звуковые тоны через production-детектор и арбитр без ASR. `speech-only` передаёт только PCM настоящей речи, без DTMF и подстановки текста. `combined` проверяет оба пути. В `debug/runs/*/audio/` сохраняются WAV для прослушивания. Расширенная проверка речи охватывает скорости 1,3 / 1,5 / 1,75 и G.711.

Стандартный входящий сценарий — DTMF первым. Отдельный явно выбранный `flow=voice` позволяет говорить с первого шага. Исходящий звонок сначала проверяет номер карты; ответ автоответчика или чужого человека не подтверждает запись. Если оператор недоступен, неэкстренное обращение запрашивает удобное время обратного звонка и сохраняется в БД. Часы задаются `OPERATOR_OPEN_HOUR`, `OPERATOR_CLOSE_HOUR`, доступность — `OPERATOR_AVAILABLE`; экстренные сигналы переводятся сразу.

## Стенд и SIP

Скопируйте `.env.example` в `.env`. Для подключения стенда задайте `APP_MODE=live`, `WITH_ML=1`, `API_TOKEN`, `STAND_URL=https://crm-med-mock.dbi-testing.ru/api/v1`, `STAND_TOKEN`, импорты расписания/пациентов и СМС-шлюз. Затем `docker compose up --build -d --wait`. В live отсутствующие модели и токен останавливают соответствующий сервис. API принимает `Authorization: Bearer …`.

Нужен экспорт пациентов и настоящее расписание с **явными doctor_id стенда**. В `data/schedule_grid.json` сохранены демонстрационные допущения исходника, это не подтверждённое расписание организаторов. Контракт: [STAND.md](docs/STAND.md).

Для Asterisk задайте разные длинные `ARI_PASSWORD`, `SIP_PHONE_PASSWORD`, `SIP_OPERATOR_PASSWORD` и выполните `docker compose --profile telephony up --build -d`. Зарегистрируйте SIP-клиент `phone` на порту 5060 и позвоните на **100**. Второй клиент `operator` получает переводы. Настройка транка и сети: [TELEPHONY.md](docs/TELEPHONY.md).

## Проверка

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
