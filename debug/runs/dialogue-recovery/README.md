Проверка 22.10.2026: изолированные runtime-тесты, без внешних записей и без реальных LLM.

Команды:

```sh
PYTHONPATH=runtime/src:. python -m pytest -q tests/integration/test_dialogue_recovery.py tests/integration/test_crm.py tests/integration/test_service_hours.py
CARGO_TARGET_DIR=/tmp/dialogue-cargo-target cargo test --offline --lib
CARGO_TARGET_DIR=/tmp/dialogue-cargo-target cargo clippy --offline --all-targets -- -D warnings
cargo fmt --check
python tools/check_space.py
```

Результат: 59 Python + 24 Rust tests passed; fmt/clippy без ошибок. Четыре новые Rust-проверки действительно проходят через Session, с корреляцией module replies: конфликт→оставить, конфликт→явное согласие→поиск нового слота, отказ от callback→заявка без звонка/СМС, runtime-сбой→дневной перевод или ночное согласие на callback.

Python проверяет временную БД, отсутствие второго POST при uncertain даже с новым ключом/слотом, ограничение consent, прошедшие визиты, отделение ошибок формы, часы 10–16/календарь, сохранение заявки без визита/СМС, блокировку reasoning в CI/Actions/act, 2s timeout, однократный bounded анализ, передачу контекста и фоновую очередь ФИО врачей с заменой устаревшего снимка.

Ограничения: голосовые/SIP/браузерные новые развилки здесь не запускались. Production reasoning API не подключён; hook по умолчанию отсутствует. Уточнение reasoner не исправляет/повторяет транзакцию автоматически: оно сохраняется в транскрипте для регистратора. Фоновый TTS bank API требует интеграцию speech-ветки. Полный список остающихся условий в TODO.md.
