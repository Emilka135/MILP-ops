# Сборка

| Компонент | Требование |
|---|---|
| Rust / Cargo | 1.98; Cargo.lock включён; `--locked` |
| Python компилятора графов | 3.11+, только stdlib |
| Python сервисов | 3.12+ из-за зафиксированного NumPy 2.5 |
| Среда | Linux x86_64; Docker Compose v2 |
| Базовые зависимости | requirements/base.txt |
| Тесты | requirements/dev.txt |
| ML | отдельные tts.txt / asr.txt / nlu.txt |
| Диск | несколько GiB для сборки, дополнительные GiB для моделей и ML-образов |

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements/dev.txt
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
pip install -r requirements/asr.txt omegaconf==2.3.0
python tools/download_models.py --tts --asr
cargo build --locked --release --bin clinic-voice
cargo test --locked
cargo build --locked --bin clinic-voice
python -m pytest tests/integration
```

Ошибка графа останавливает сборку. Править сгенерированный Rust не нужно: источник — `scenarios/*.flow`.

Веса загружаются до развёртывания `tools/download_models.py`; сохраняются URL, ревизии и SHA256. Модели, SQLite, секреты и временные отчёты исключены из Git/Docker build context; модели монтируются read-only.

Обычная сборка: `docker compose build`. В локальном окружении bridge-сеть Docker не разрешала внешний DNS на этапе build, поэтому проверка использовала временный override `build.network: host`. Основной Compose сохраняет стандартный переносимый режим. Host networking отдельно используется Asterisk для SIP/RTP.

Asterisk 22.11.0 собирается из официального tar.gz с зафиксированным SHA256; готовый пакет Asterisk в Debian Bookworm отсутствует. Dockerfile сохраняет COPYING в образе. Исходник: https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz
