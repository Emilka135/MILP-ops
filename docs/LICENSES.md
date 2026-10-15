# Лицензии и политика включения

MIT приложения не распространяется автоматически на веса, данные и внешний runtime. Сохранены исходный LICENSE MILP-ops и `docs/upstream/calls-LICENSE`. Права на каждый дополнительный компонент проверяются отдельно.

| Лицензии | Политика проекта | Условие |
|---|---|---|
| MIT, BSD-2/3-Clause, Apache-2.0, ISC, Unicode-3.0 | разрешены для зависимостей ядра | сохранять notices и выполнять условия конкретной лицензии |
| PSF-2.0 | разрешена для Python | условия Python и поставляемых библиотек |
| LGPL-3.0-or-later / GPL-2.0 | внешний процесс/контейнер | отдельный процесс не отменяет обязательств при распространении бинарников/изменений |
| CC BY-NC-SA 4.0 | веса Silero v5.5: отдельное решение о допустимости использования | не считать MIT; для использования вне разрешённых условий нужен соответствующий договор |
| неизвестная / отсутствующая | не включать | сначала получить проверяемые условия |

| Компонент | Лицензия / первичный источник | Использование |
|---|---|---|
| Rust-код приложения | MIT, LICENSE | VM, очереди, API |
| MILP-ops / calls-arena | MIT, сохранённые LICENSE | исходные модули и изменения |
| Tokio, Axum, Hyper, Serde | MIT/Apache-2.0 согласно пакетам Cargo.lock | зависимости ядра; CI cargo-deny |
| NumPy | BSD-3-Clause | аудио и NLU |
| openpyxl | MIT | импорт XLSX |
| PyTorch | BSD-3-Clause | TTS, optional BGE |
| ONNX Runtime | MIT | локальный ASR |
| onnx-asr | MIT, [репозиторий](https://github.com/istupakov/onnx-asr) | ONNX-адаптер |
| GigaAM / веса v3 | MIT, [репозиторий авторов](https://github.com/salute-developers/GigaAM) | локальное распознавание |
| T-one | Apache-2.0, [лицензия](https://github.com/voicekit-team/T-one/blob/main/LICENSE) | исследованная альтернатива |
| deepvk/USER-bge-m3 | Apache-2.0, model card: [первичный источник](https://huggingface.co/deepvk/USER-bge-m3) | optional NLU на CPU, веса не включены |
| **Silero v5_5_ru** | **CC BY-NC-SA 4.0**, [файл LICENSE](https://github.com/snakers4/silero-models/blob/master/LICENSE) | модель заказчика, скачивается отдельно |
| Silero CIS base | MIT, [LICENSE_CIS](https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS) | возможная замена через TTS_MODEL и голос ru_* |
| Deeray runtime | LGPL-3.0-or-later, [COPYING.LESSER](https://github.com/deeray-ltd/gigaam-v3-runtime/blob/main/COPYING.LESSER) | исследован; код/бинарник не встроен |
| Asterisk | GPL-2.0, [лицензирование](https://www.asterisk.org/products/software/licensing/) | отдельный SIP-процесс |

README Silero использует сокращённое описание условий; здесь приоритет отдаётся полному файлу LICENSE. Веса не публикуются вместе с кодом. Лицензии статей доступны на соответствующих страницах arXiv; в репозитории содержатся ссылки и собственные краткие описания, не полные тексты статей или сторонний неопубликованный код.

CI проверяет лицензии Rust через cargo-deny; SCA и SBOM решают отдельные задачи и не заменяют проверку лицензии весов. Таблица — политика состава проекта, а не заключение о допустимости любого конкретного внедрения.
