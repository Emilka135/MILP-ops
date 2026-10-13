# Архитектура и контракт модулей

Прототип голосового ассистента регистратуры ГБУЗ «Городская поликлиника № 7»
(хакатон «Точка сборки», кейс № 2 «Голосовые интерфейсы»).

Документ описывает **границы модулей и сигнатуры** — то, о чём договариваются
исполнители. Внутреннее устройство модуля исполнитель выбирает сам.

## 1. Схема

```
[Микрофон браузера / текст] → ASR (Web Speech API, оффлайн-локально)
        │
        ▼
app/web/server.py  — HTTP + статика + журнал
        │
        ▼
app/assistant/engine.py  DialogManager (конечный автомат + слот-филлинг)
        │
        ├─ app/safety/detector.py  SafetyLayer  ← ПЕРВЫМ делом, до любого решения
        ├─ app/assistant/nlu.py    NLU по правилам (без внешних LLM)
        ├─ app/kb/knowledge.py     база знаний из регламента
        ├─ app/assistant/slot_engine.py  SlotEngine (свободные слоты)
        └─ app/mis/client.py       MisClient (стенд «Регистратура»)
                 └─ app/mis/mock_stand.py  оффлайн-стенд (когда токена нет)
        │
        ▼
[Шаблоны реплик из регламента] → TTS (Web Speech API)
        │
        ▼
app/web/static/ — панель демо: «что понял ассистент», «вызовы API»,
                  «карточка перевода», метрики, экран оператора
```

Safety-слой проверяет **каждую** реплику пациента и стоит перед любой другой
логикой. Решения о красных флагах, «операторе» и медицинских вопросах не
делегируются модели.

## 2. Границы и владельцы файлов

| Область | Файлы | Владелец |
|---|---|---|
| Контракт данных | `app/types.py` | Lead (заморожен) |
| Слот-движок, аналитика | `app/assistant/slot_engine.py`, `tools/build_schedule.py`, `tools/analyze_data.py`, `data/*` | teammate-a |
| MIS-клиент + оффлайн-стенд | `app/mis/client.py`, `app/mis/mock_stand.py`, `tests/test_mis.py` | teammate-b |
| База знаний + safety | `app/kb/knowledge.py`, `app/kb/*.json`, `app/safety/detector.py`, `tests/test_safety.py` | teammate-c |
| Веб-интерфейс | `app/web/static/*` | teammate-d |
| Диалоговый менеджер, HTTP-сервер, интеграция | `app/assistant/*.py`, `app/web/server.py` | Lead |
| Приёмочные тесты сценариев, README, демо | `tests/acceptance.py`, `README.md`, `DEMO.md` | Lead + проверка всеми |

Правило: **один файл — один владелец**. Нужна правка в чужом файле — писать
владельцу, не редактировать самому.

## 3. Замороженные контракты

Все структуры — в `app/types.py`. Ключевые:

- `Doctor`, `DoctorSchedule`, `Slot`, `SlotQuery` — справочник, расписание, слоты;
- `Client`, `Visit`, `ApiCall`, `MisError`, `MisTimeout` — стенд;
- `KbEntry` — база знаний;
- `SafetyMatch`, `SafetyVerdict` — решение safety-слоя;
- `DialogState`, `TurnResult` — состояние и результат хода диалога;
- формат дат стенда: `fmt_date`, `fmt_datetime` (`ДД.ММ.ГГГГ`, `ДД.ММ.ГГГГ ЧЧ:ММ`),
  часовой пояс GMT+4; `VisitStatus`, `Outcome`, `Priority`, `Intent`.

### 3.1. `app/mis/client.py` (teammate-b)

```python
class MisClient:
    def __init__(self, *, token: str = "", base_url: str = BASE_URL,
                 store=None, timeout: float = 5.0,
                 calls: list[ApiCall] | None = None,
                 mock: bool = False, scenario: str = "") -> None: ...

    @property
    def offline(self) -> bool: ...          # True — работает оффлайн-стенд

    # каждый метод пишет ApiCall в self.calls (self.calls — тот же список,
    # что передал владелец; если None — создаёт свой, доступный как .calls)
    def get_doctors(self, *, specialty: str | None = None,
                    force: bool = False) -> list[Doctor]: ...
    def find_patient(self, last_name: str, first_name: str,
                     birth_date: str) -> Client | None: ...
    def search_clients(self, **filters) -> list[Client]: ...
    def create_patient(self, last_name: str, first_name: str, birth_date: str,
                       middle_name: str | None = None,
                       comment: str | None = None) -> Client: ...
    def patch_patient(self, client_id: int, **fields) -> Client: ...
    def get_patient(self, client_id: int) -> Client: ...
    def list_visits(self, *, client_id: int | None = None,
                    status: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, doctor_id: int | None = None,
                    fresh: bool = False) -> list[Visit]: ...
    def get_visit(self, visit_id: int) -> Visit: ...
    def create_visit(self, client_id: int, doctor_id: int,
                     scheduled_at: str) -> Visit: ...
    def patch_visit(self, visit_id: int, *, doctor_id: int | None = None,
                    scheduled_at: str | None = None,
                    status: str | None = None) -> Visit: ...
    def set_scenario(self, scenario: str) -> None: ...   # подпись в ApiCall.scenario
```

Требования стенда, которые обязан закрыть клиент:

- заголовок `Authorization: Bearer <token>`;
- **лимит 10 запросов/мин**: собственный ограничитель + разбор `429` и
  `Retry-After`; при исчерпании — ждать и повторить;
- `400/401/404/409` → `MisError` с `status`, `code` (поле `code` из тела),
  `message` (русский текст стенда);
- таймаут > 5 с → `MisTimeout` (это основание для правила R-06);
- кэш справочника врачей (в т. ч. на диске, `var/doctors_cache.json`);
- пагинация `limit`/`offset` (максимум 50 объектов на страницу);
- **никогда** не пишет ФИО/дату рождения в логи `ApiCall`;
- если `token` пуст или стенд недоступен при старте — переключается в
  оффлайн-режим (`mock=True`) и пишет `ApiCall(source="MOCK")`.

`app/mis/mock_stand.py` — тот же контракт поверх локального JSON-файла
(`var/mock_stand.json`), чтобы демо и автотесты работали без токена.
Семантика обязана совпадать со стендом: три статуса, отменённую запись менять
нельзя, к деактивированному врачу запись и перенос запрещены (400).

### 3.2. `app/assistant/slot_engine.py` (teammate-a)

```python
class SlotEngine:
    def __init__(self, *, schedule_path="data/schedule_grid.json",
                 doctors_path="data/doctors.json",
                 specialties_path="data/specialties.json",
                 horizon_days: int = 14, now_provider=None) -> None: ...

    def doctors(self) -> list[Doctor]: ...
    @property
    def is_ready(self) -> bool: ...          # False — если data/* ещё не собраны
    def doctor_days(self, doctor_id: int) -> dict[int, tuple[str, ...]]: ...
    def doctor_shift(self, doctor_id: int) -> tuple[str, str] | None: ...
    def find_slots(self, query: SlotQuery, *, busy=None) -> list[Slot]: ...
    def resolve_specialty(self, text: str) -> str | None: ...
    def doctors_of_specialty(self, specialty: str) -> list[Doctor]: ...
    def waiting_terms(self) -> dict[str, int]: ...   # сроки ПГГ, рабочих дней
```

- `busy` — список занятых моментов: `list[tuple[int, datetime]]` (врач, начало)
  или `list[Visit]`; принимает оба вида.
- Часы работы поликлиники: будни 7:30–20:00, суббота 9:00–15:00, воскресенье —
  выходной. Слот «сегодня» доступен, если до него ≥ 30 минут.
- Горизонт — 14 календарных дней, по умолчанию `limit=3`.
- Файлы данных:

```jsonc
// data/schedule_grid.json
{"generated_at": "...", "horizon_days": 14,
 "schedules": [ <DoctorSchedule.to_dict()>, ... ]}

// data/doctors.json
{"generated_at": "...", "source": "история записей | API | Excel",
 "doctors": [ {"id": 1, "full_name": "...", "specialty": "терапевт",
               "is_active": true, "shift": {"start": "08:00", "end": "14:30"},
               "step_minutes": 20}, ... ]}

// data/specialties.json
{"default": "терапевт",
 "primary_care": ["терапевт", "педиатр"],
 "referral_required": ["кардиолог", "невролог", "эндокринолог",
                       "гастроэнтеролог", "уролог", "пульмонолог"],
 "direct": ["терапевт", "педиатр", "хирург", "офтальмолог",
            "оториноларинголог", "акушер-гинеколог", "дерматовенеролог",
            "травматолог-ортопед"],
 "synonyms": {...}}
```

### 3.3. `app/safety/detector.py` (teammate-c)

```python
class SafetyLayer:
    def __init__(self, *, glossary_path="app/safety/red_flags.json") -> None: ...
    def check(self, text: str, *, context: dict | None = None) -> SafetyMatch: ...
    def boundary_phrase(self, topic: str = "") -> str: ...
```

- Категория А → `SafetyVerdict.RED_FLAG_A`, `rule="R-01"` (раздел 7.4, словарь групп:
  сердце/грудь, дыхание, сознание, инсульт, судороги, кровотечение, травмы и
  отравления, аллергия, дети, беременность, угроза себе или другим).
- Категория Б → `RED_FLAG_B`, `rule="R-11"` (острая боль, температура 40,
  резкое ухудшение, травма без угрозы жизни).
- `OPERATOR` — «оператор/человек/сотрудник/регистратор/живой/соедини».
- `COMPLAINT` — жалоба, агрессия, «пожаловаться», «безобразие», «нагрубил».
- `MEDICAL` — симптомы, лекарства, «что выпить», «какой у меня холестерин»,
  просьба продиктовать результат.
- `RECORD_OPT_OUT` — «не записывайте», «я против записи».
- `SPD_CONSENT` — отзыв согласия на обработку ПДн, удаление данных.
- `SILENCE` — пустая реплика/`[тишина]`.
- Работает **по смыслу**: нормализация (регистр, ё/е, опечатки), стемминг
  словоформ, отрицание («не болит» не флаг, «нет боли в груди» не флаг),
  прошедшее время чужого опыта → сначала фраза про 103, но сценарий не рвать
  (флаг `soft=True` в `reason`/`debug` — согласовать в `SafetyMatch.reason`).
- `check("")` → `SILENCE`. Никогда не выбрасывает исключение.

### 3.4. `app/kb/knowledge.py` (teammate-c)

```python
class KnowledgeBase:
    def __init__(self, *, path="app/kb/kb.json") -> None: ...
    def answer(self, question: str, *, patient: Client | None = None,
               mis: MisClient | None = None) -> KbEntry | None: ...
    def topic(self, key: str) -> KbEntry | None: ...
    def prices(self) -> dict[str, int]: ...
    def working_hours(self) -> dict[str, str]: ...
    def lab_terms(self) -> dict[str, str]: ...
```

- Источник — разделы 2.1–2.6, 6.1–6.9 регламента. Ответы — короткие, для
  произнесения вслух, без аббревиатур, с предложением следующего шага.
- Реплики ассистента ≤ 20 слов, ≤ 3 вариантов выбора.
- `answer()` не выдумывает: нет ответа в справочнике → `None`, вызывающий
  переводит на регистратора (R-08/R-10).

### 3.5. `app/web/static/` (teammate-d)

Одностраничное демо на русском, без сборщиков и внешних CDN.

Обязательные панели:

1. **Диалог**: лента реплик «Пациент / Ассистент», поле ввода текста и кнопка
   микрофона. Если `SpeechRecognition`/`speechSynthesis` недоступны — показать
   причину и работать в текстовом режиме (голос не обязателен для демо).
2. **Что понял ассистент**: интент (код и название), сценарий (С-xx), состояние
   автомата, собранные параметры, safety-вердикт и код правила (R-xx).
3. **Вызовы API**: живой журнал — время, метод, путь, код ответа, длительность,
   подпись; отдельно помечать `source=MOCK` (оффлайн-стенд) и `source=MIS`.
   Именно эту панель жюри сопоставляет с журналом обращений к API стенда.
4. **Карточка перевода**: поля Приложения В — ID звонка, время, номер
   (маскированный), пациент, интент, собранные параметры, причина (R-xx),
   приоритет, резюме, недовольство, согласие на запись.
5. **Экран оператора**: очередь переводов, карточка выбранного перевода,
   кнопки «Принять», «Итог».
6. **Метрики и аналитика**: из `data/metrics.json` — пропущенные звонки,
   неявки, длительности, экономика; расчёт виден в интерфейсе.
7. **Сценарии демо**: 8 кнопок из раздела 7 ТЗ, запускают
   `POST /api/demo/{n}` и подставляют реплики пациента.

### 3.6. HTTP-сервер (Lead, `app/web/server.py`)

```
GET  /                     → app/web/static/index.html + ассеты
GET  /api/health           → {ok, offline, token_set, doctors, horizon_days}
POST /api/session          → {session_id}
POST /api/turn             → body {session_id, text|audio_text}  → TurnResult.to_dict()
GET  /api/session/{id}     → {session: DialogState.to_dict(), actions: [...], transfers: [...]}
POST /api/session/{id}/reset
GET  /api/doctors          → справочник (кэш) + наши смены/шаги
GET  /api/slots?specialty=&date_from=&part_of_day= → list[Slot]
GET  /api/metrics          → data/metrics.json
GET  /api/kb?q=            → KbEntry
GET  /api/transfers        → очередь карточек перевода
POST /api/transfers/{id}/accept
POST /api/demo/{n}         → прогон демо-сценария n, возвращает {steps: [...], session_id}
GET  /api/tests            → результат последнего прогона контрольных диалогов
```

## 4. Правила безопасности, обязательные для всех

1. Красный флаг категории А прерывает **любой** сценарий в **любой** реплике.
2. «Оператор/человек/сотрудник» → немедленный перевод, без уговоров.
3. Ассистент не ставит диагноз, не советует препараты, не озвучивает значения
   анализов (только «готово / не готово»).
4. «Пациент называет первым»: ФИО, дату рождения и сведения о записях ассистент
   не озвучивает до идентификации; постороннему о записях не сообщает.
5. В журналах и логах нет ФИО/даты рождения открытым текстом — только
   хэши/маски и идентификаторы.
6. Реплики ≤ 20 слов, ≤ 3 вариантов выбора, без «нажмите 1».
7. Перенос: сначала создать новую запись, **затем** отменить старую. Если
   создание не удалось — старая остаётся в силе (R-06).

## 5. Запуск

```
python app/web/server.py            # нужен токен: см. README
python tests/acceptance.py          # контрольные диалоги + приёмочные сценарии
```
