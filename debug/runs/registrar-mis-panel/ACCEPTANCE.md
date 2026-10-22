# Рабочее место регистратора: локальная приёмка

Рабочее место перенесено по единственному пользовательскому эталону `adjust-web-page-styles (1).zip` (index.html, 316396 bytes). Визуальная тема, компактные панели и docking адаптированы; MIT notice сохранён в `scenario-editor/THIRD_PARTY_NOTICES.md`. Подставные роли, sql.js/клиентская база, speechSynthesis и демозаписи прототипа не перенесены. Существующий административный редактор и ObserverCall.jsx не редактировались.

## Проверки

`node debug/run_registrar_mis_panel.mjs` — **22 passed**, настоящий Chromium и настоящий clipboard, синтетические HTTP fixtures (`result.json`):

- исходное расположение звонков слева, ответа справа, разговора в центре;
- перетаскивание мышью к каждому из четырёх краёв и в отдельное окно; React-поддерево инспектора остаётся тем же DOM-элементом, вызов/выбор запроса не перемонтируются;
- ширина/высота, pointer + keyboard, Escape, доступные списки расположения, reset;
- сохранение расположения, размеров и темы после reload; светлая/тёмная/системная тема ограничены рабочим местом;
- 900px без наложения панелей и 390px с последовательной раскладкой без горизонтального переполнения страницы;
- точное копирование ФИО, NULL, JSON, пустой строки, длинного значения, SQL и параметров; настоящий execCommand fallback и видимая ошибка полного отказа;
- пустой и несохранённый ответ различаются; поздний ответ открытия не заменяет выбранный звонок; предупреждения архива не теряются;
- «Схемы / Справка» отображает точный текст документа из репозитория и не предлагает административный редактор наблюдателю.

`node debug/run_registrar_scenario_actions.mjs` — **8 passed**, настоящий editor HTTP, Repository, Learning/SQLite и knowledge API, без route.fulfill (`scenario-actions/result.json`): admin save/restore; документы и шаги реального пакета scenarios; scoped-предложение сотрудника из просмотра схемы, queued line100; чужая line101 пуста, общий проект неизменен; настоящие серверные ACL; ночь только prepared, review blocked; SQLite подтверждает две метки и ноль записей на приём.

`PYTHONPATH=runtime/src:. python -m pytest -q tests/integration/test_call_archive.py tests/integration/test_workshop_access.py tests/integration/test_learning.py tests/contracts/test_scenario_knowledge.py` — **25 passed** (`server-tests.log`).

`npm run build --prefix scenario-editor` — passed (`build.log`, убраны только ANSI-коды и пробелы в конце строк). Остались предупреждения upstream use-client и размер JS chunk. Контроль диска до/после passed (`space-after.json`).

## Визуальная проверка

Просмотрены screenshots 01–05 и scenario-actions/01–05. Светлая и тёмная темы читаемы; floating-панель доступна; на телефоне панели складываются. Старые PNG admin save/restore остались побайтно неизменными; main.jsx, style.css и ObserverCall.jsx не редактировались.

## Границы доказательств

Первый runner доказывает UI на явно синтетических HTTP-ответах. Второй доказывает настоящий путь административных/серых правок и чтения справки, но встроенный runtime-архив синтетический: ASR и исполнение FSM этим прогоном **не** заявляются. Для записи настоящего Session поддержан SCENARIO_ACTIONS_ARCHIVE с независимым expected_target. Внешние МИС/модели и данные пациентов не нужны. Выполнение в act/Actions и общий аудиопрогон остаются отдельной интеграционной приёмкой; локальные тесты за них не выдаются.
