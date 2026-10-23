# Образцы входных файлов

| файл | что это | переменная окружения | как грузится |
|---|---|---|---|
| `schedule.csv` | расписание врачей (организаторы дают Excel — те же колонки, `.xlsx` читается через openpyxl) | `CRM_SCHEDULE` | `MockRegistry.load_schedule_csv/xlsx` → слоты по `slot_minutes` |
| `patients.csv` | пациенты (СНИЛС/карта/телефон в любом написании — нормализуются; `id` = id пациента в стенде, необязателен) | `CRM_PATIENTS` | `load_patients_csv/xlsx`, идемпотентно по СНИЛС/карте |
| `bookings.csv` | записи, которые уже есть в стенде (сделаны регистратором), с **id стенда** — нужны для напоминаний/отмены/переноса | `CRM_BOOKINGS` | `load_bookings_csv/json/xlsx` или `StandSync.pull_bookings(day)` (1 GET) |

```bash
CRM_SCHEDULE=data/samples/schedule.csv CRM_PATIENTS=data/samples/patients.csv CRM_BOOKINGS=data/samples/bookings.csv \
  python -m calls.services crm --port 8081
```
