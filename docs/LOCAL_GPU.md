# Локальный Docker на NVIDIA

Команда: `./clinic start --gpu`. Эквивалент без локального дополнительного файла: `docker compose -f compose.yaml -f compose.gpu.yaml up --build -d --wait orchestrator scenario-editor`.

ASR: ONNX Runtime GPU 1.31.0, GigaAM-v3-e2e-RNNT, CUDAExecutionProvider. CUDA13/cuDNN поставляются внутри отдельного образа, системный toolkit устанавливать для моделей не нужно. NVIDIA Container Toolkit на хосте должен быть установлен. TTS: Silero CIS base MIT, CPU, аудиобанк; голос в браузере48кГц, телефон8кГц. Использование GPU не исправляет ударения: за них отвечает отдельная проверка текста.

Нужен `v3_e2e_rnnt_encoder.onnx` в modules/asr/models. Исходный int8 encoder сохраняется для CPU. Параметр `ASR_GPU_QUANTIZATION` позволяет явно выбрать вариант, но CUDA-проверка отклонит модель, если основные операции ушли на CPU.

При запуске ASR проверяет реальные провайдеры созданных сессий, отключает автоматический fallback и на первом аудиозапросе проверяет профили основных нейронных операций. FFT/предобработка выполняется NumPy на CPU; encoder/decoder/joiner должны действительно использовать CUDA. Артефакты — debug/runs/local-gpu/profiles. Это проверка устройства, а не оценка качества речи.

`ASR_GPU_MEM_LIMIT_MIB=1024` ограничивает арену каждой модельной сессии, а не всю VRAM процесса. CUDA-контекст и библиотеки требуют память сверх этого; сессий три. Запросы одного экземпляра распознавателя сериализованы. `CLINIC_GPU_DEVICE=0` выбирает физическую карту, внутри контейнера она видна какCUDA0. Чужие процессы не останавливаются.

На текущем хосте driver580.178.04 и RTX A5000 Laptop16ГБ. Docker `--gpus` обращался к устаревшей CDI-спецификации graphics с библиотекой580.173.02. Явный `runtime: nvidia`, NVIDIA_VISIBLE_DEVICES и capabilities compute,utility позволяет запуск без исправления глобальной конфигурации/перезапуска драйвера. Overlay применяет этот проверенный путь.

Сборка хранит wheel-кэш на Docker-разделе. Изменение Python/сценария не требует повторного скачивания CUDA; первая сборка загружает большой runtime. При переносе checkout используйте новый каталог проекта: CLI вычисляет путь относительно себя. `.env` не перезаписывайте; пароль VM для локального запуска не нужен. Демо не пишет в настоящий DBI, даже если ключ сохранён.

18.10.2026: реальная CUDA-приёмка прошла: «Запишите меня к терапевту», encoder210/decoder13/joiner120 нейронных CUDA-операций, ноль тяжёлых CPU-операций. FP32-отчёт: debug/runs/local-gpu/profiles/fp32-asr.json. Int8 был проверен отдельно и отклонён из-за CPU ConvInteger/MatMulInteger/LSTM; этот неуспешный отчёт сохранён. Явный provider в конфигурации не является доказательством — проверены сами профили.

Локальный Compose запущен, все обязательные сервисы healthy, audio-check завершился0. Собственный ASR занимал около1398MiB VRAM после прогрева; это наблюдение, не предел для любой длины/нагрузки. Короткий образец1,102с на первом запросе RTF0,241; это не обещание общей пропускной способности. Пакеты образа: debug/runs/local-gpu/gpu-python-freeze.txt. Браузерная проверка с EXPECT_CUDA=1 фиксируется отдельно в debug/runs/local-gpu/speech-ui/hosted/.

Источники установки: [ONNX Runtime CUDA](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html), [onnx-asr](https://github.com/istupakov/onnx-asr).
