/* =============================================================================
 * Демо-интерфейс голосового ассистента регистратуры поликлиники № 7.
 *
 * Только vanilla JS, без сборщиков и без внешних запросов.
 * Контракт: docs/ARCHITECTURE.md (3.5, 3.6) + app/types.py.
 *
 * Структура файла:
 *   1) справочники и подписи
 *   2) состояние
 *   3) HTTP-слой и журнал «Вызовы API»
 *   4) панель «Что понял ассистент»
 *   5) карточка перевода (Приложение В)
 *   6) экран оператора
 *   7) метрики и экономика
 *   8) диалог, сценарии, голос (Web Speech API)
 * ========================================================================== */
'use strict';

(function () {
  /* ==========================================================================
   * 1. Справочники и подписи
   * ======================================================================== */

  var INTENT_NAMES = {
    'I-01': 'Запись на приём',
    'I-02': 'Перенос записи',
    'I-03': 'Отмена записи',
    'I-04': 'Проверка своей записи',
    'I-05': 'Вызов врача на дом',
    'I-06': 'График врача / поликлиники',
    'I-07': 'Готовность анализов',
    'I-08': 'Платные услуги',
    'I-09': 'Лист ожидания',
    'I-10': 'Прикрепление, полис, документы',
    'I-11': 'Экстренная ситуация',
    'I-12': 'Жалоба, конфликт',
    'I-13': 'Медицинский вопрос',
    'I-14': 'Прочее / нестандартное',
    'R-02': 'Просьба соединить с человеком',
    'UNKNOWN': 'Не распознано'
  };

  var SCENARIO_NAMES = {
    'С-01': 'Запись на приём (прямая)',
    'С-02': 'Запись по направлению',
    'С-03': 'Перенос записи',
    'С-04': 'Отмена записи',
    'С-05': 'Проверка своей записи',
    'С-06': 'Вызов врача на дом',
    'С-07': 'Справка о графике работы',
    'С-08': 'Готовность анализов',
    'С-09': 'Платные услуги',
    'С-10': 'Исходящий звонок-напоминание',
    'С-11': 'Лист ожидания',
    'С-12': 'Звонок в интересах ребёнка или другого человека',
    'С-13': 'Прикрепление к поликлинике (справка)',
    'С-14': 'Экстренная ситуация',
    'С-15': 'Жалоба'
  };

  var STEP_NAMES = {
    INTRO: 'приветствие',
    START: 'начало',
    IDENTIFY: 'идентификация пациента',
    IDENTIFICATION: 'идентификация пациента',
    SPECIALTY: 'выбор специальности',
    OFFER_SLOTS: 'предложение слотов',
    SLOTS: 'предложение слотов',
    CONFIRM: 'подтверждение',
    CONFIRMATION: 'подтверждение',
    CREATE_VISIT: 'создание записи в МИС',
    BOOKED: 'запись создана',
    RESCHEDULE: 'перенос записи',
    CANCEL: 'отмена записи',
    INFO: 'справка',
    KB: 'ответ из базы знаний',
    SAFETY_STOP: 'остановка по красному флагу',
    TRANSFER: 'перевод на оператора',
    OPERATOR: 'перевод на оператора',
    DONE: 'завершено',
    FINISHED: 'завершено',
    SILENCE: 'тишина / помехи',
    WRAP_UP: 'завершение звонка',
    OUTBOUND: 'исходящий звонок'
  };

  var SAFETY_UI = {
    ok: { label: 'норма', cls: 'safety-ok', badge: 'badge-ok', note: 'Срабатываний нет: обычная реплика.' },
    red_flag_a: { label: 'КРАСНЫЙ ФЛАГ А', cls: 'safety-alarm', badge: 'badge-err', note: 'Угроза жизни: немедленно 103/112, перевод P1 (R-01).' },
    red_flag_b: { label: 'КРАСНЫЙ ФЛАГ Б', cls: 'safety-alarm', badge: 'badge-err', note: 'Острая боль / резкое ухудшение: регистратор, приоритет P1 (R-11).' },
    operator: { label: 'просьба оператора', cls: 'safety-warn', badge: 'badge-warn', note: 'Пациент просит человека — немедленный перевод, без уговоров (R-02).' },
    complaint: { label: 'жалоба / конфликт', cls: 'safety-warn', badge: 'badge-warn', note: 'Жалоба или агрессия: перевод старшему администратору (R-03).' },
    medical: { label: 'медицинский вопрос', cls: 'safety-warn', badge: 'badge-warn', note: 'Ассистент не ставит диагноз и не советует препараты: граничная фраза.' },
    spd_consent: { label: 'отзыв согласия ПДн', cls: 'safety-warn', badge: 'badge-warn', note: 'Отзыв согласия на обработку ПДн или удаление данных (R-16).' },
    record_opt_out: { label: 'отказ от записи разговора', cls: 'safety-warn', badge: 'badge-warn', note: 'Аудио не сохраняется, стенограмма недоступна.' },
    silence: { label: 'тишина / помехи', cls: 'safety-warn', badge: 'badge-warn', note: 'Пустая реплика или помехи (R-17).' },
    third_party: { label: 'сведения о другом человеке', cls: 'safety-warn', badge: 'badge-warn', note: 'Постороннему сведения о записях не сообщаются.' }
  };

  var PRIORITY_UI = {
    P1: { label: 'P1 · экстренный', note: 'не более 20 секунд', cls: 'prio-P1' },
    P2: { label: 'P2 · высокий', note: 'не более 30 секунд', cls: 'prio-P2' },
    P3: { label: 'P3 · обычный', note: 'не более 90 секунд', cls: 'prio-P3' }
  };

  /* Приложение Г регламента — коды результата звонка */
  var OUTCOMES = [
    ['BOOKED', 'Создана запись'],
    ['BOOKED_GP', 'Записан к участковому за направлением'],
    ['BOOKED_PAID', 'Создана платная запись'],
    ['RESCHEDULED', 'Запись перенесена'],
    ['CANCELLED', 'Запись отменена'],
    ['CONFIRMED', 'Визит подтверждён (исходящий)'],
    ['INFO_GIVEN', 'Предоставлена справка'],
    ['WAITLISTED', 'Пациент в листе ожидания'],
    ['HOME_VISIT_REQUESTED', 'Заявка на вызов врача на дом'],
    ['ESCALATED', 'Звонок переведён сотруднику'],
    ['CALLBACK', 'Задача на обратный звонок'],
    ['EMERGENCY', 'Экстренная маршрутизация (R-01)'],
    ['COMPLAINT', 'Жалоба зарегистрирована'],
    ['GRATITUDE', 'Благодарность зарегистрирована'],
    ['DROPPED', 'Пациент положил трубку'],
    ['WRONG_PERSON', 'Исходящий: ошибся номером'],
    ['VOICEMAIL', 'Исходящий: автоответчик'],
    ['NO_ANSWER', 'Исходящий: не ответили'],
    ['OPT_OUT', 'Исходящий: отказ от обзвона']
  ];

  var OUTCOME_LABELS = {};
  OUTCOMES.forEach(function (pair) { OUTCOME_LABELS[pair[0]] = pair[1]; });

  var SLOT_LABELS = {
    specialty: 'специальность',
    doctor_id: 'врач (ID)',
    doctor_name: 'врач',
    doctor: 'врач',
    date: 'дата',
    date_from: 'дата, с',
    time: 'время',
    start: 'начало',
    end: 'окончание',
    part_of_day: 'часть дня',
    time_from: 'время, с',
    time_to: 'время, до',
    last_name: 'фамилия',
    first_name: 'имя',
    middle_name: 'отчество',
    full_name: 'ФИО',
    birth_date: 'дата рождения',
    phone: 'телефон',
    direction: 'направление',
    referral: 'направление',
    reason: 'повод',
    cabinet: 'кабинет',
    visit_id: 'запись (ID)',
    client_id: 'пациент (ID)',
    paid: 'платно',
    home_visit: 'вызов на дом',
    count: 'вариантов',
    limit: 'лимит вариантов',
    weekdays: 'дни недели',
    exclude_weekdays: 'кроме дней',
    date_to: 'дата, по',
    slot: 'выбранный слот',
    confirmed: 'подтверждено',
    /* Ключи DialogState.pending из app/assistant/engine.py */
    greeted_patient: 'пациент поприветствован',
    chosen_slot: 'выбранный слот',
    wanted_date: 'желаемая дата',
    wanted_doctor_id: 'желаемый врач (ID)',
    banned_weekdays: 'исключить дни недели',
    via_gp: 'через участкового',
    booked_gp_code: 'код BOOKED_GP',
    specialist_without_referral: 'специалист без направления',
    existing_visit_id: 'активная запись (ID)',
    offer_reschedule: 'предложить перенос',
    no_slots: 'свободных слотов нет',
    active_visits: 'активные записи',
    target_visit_id: 'выбранная запись (ID)',
    target_visit: 'запись к отмене',
    old_visit: 'прежняя запись',
    same_doctor: 'тот же врач',
    transfer_offered: 'предложен перенос',
    check_reported: 'запись озвучена',
    address: 'адрес',
    address_masked: 'адрес (маска)',
    can_come: 'может прийти',
    outbound_refuse: 'отказ от напоминания',
    outbound_visit: 'запись для обзвона',
    medical_repeat: 'повтор медицинского вопроса',
    reschedule: 'перенос',
    cancel: 'отмена',
    visit_status: 'статус записи',
    check_hint: 'подсказка по записи'
  };

  var METRIC_LABELS = {
    missed_calls: 'пропущенные звонки',
    missed: 'пропущенные звонки',
    missed_share: 'доля пропущенных звонков',
    missed_rate: 'доля пропущенных звонков',
    missed_by_hour: 'пропущенные по часам',
    total_calls: 'всего звонков',
    calls_total: 'всего звонков',
    calls: 'звонки',
    no_show: 'неявки на приём',
    no_shows: 'неявки на приём',
    no_show_rate: 'доля неявок на приём',
    no_show_share: 'доля неявок на приём',
    no_show_by_specialty: 'неявки по специальностям',
    avg_wait_seconds: 'среднее ожидание на линии, с',
    wait_avg: 'среднее ожидание на линии, с',
    peak_wait_seconds: 'пиковое ожидание на линии, с',
    avg_duration_minutes: 'средняя длительность звонка, мин',
    durations: 'длительности по типам звонка',
    avg_duration_by_type: 'средняя длительность по типам',
    autonomous_share: 'обработано без человека',
    without_human_share: 'обработано без человека',
    transfers: 'переводы на оператора',
    transfers_share: 'доля переводов',
    economics: 'экономика',
    economy: 'экономика',
    saved_minutes: 'сэкономленные минуты',
    saved_minutes_per_call: 'сэкономлено минут на звонок',
    cost_per_minute: 'стоимость минуты оператора, ₽',
    operator_cost_per_minute: 'стоимость минуты оператора, ₽',
    savings_rub: 'экономия, ₽',
    savings: 'экономия, ₽',
    tariff: 'тариф, ₽/мин',
    formula: 'формула',
    formulas: 'формулы',
    assumptions: 'допущения',
    notes: 'примечания',
    source: 'источник данных',
    generated_at: 'рассчитано',
    period: 'период данных',
    by_result: 'итоги по кодам результата',
    by_outcome: 'итоги по кодам результата',
    results: 'итоги по кодам результата',
    automated_share: 'обработано без человека',
    automation_share: 'обработано без человека',
    by_result_share: 'доли по кодам результата',
    per_day: 'в день',
    insights: 'наблюдения',
    sources: 'источники',
    month: 'месяц',
    name: 'название',
    hour: 'час',
    hours: 'часы',
    total: 'всего',
    appointments_days: 'дней с записями',
    slot_engine_ready: 'сетка слотов собрана',
    mis_calls: 'вызовов к стенду',
    data_dir: 'каталог данных',
    server_time: 'время сервера',
    hint: 'подсказка',
    error: 'ошибка',
    status: 'статус',
    passed: 'пройдено',
    total: 'всего',
    failed: 'провалено'
  };

  /* Демо-сценарии: POST /api/demo/{n}. Порядок 1–8 — демо-скрипт ТЗ (раздел 7). */
  var DEMO_SCENARIOS = [
    { n: 1, code: 'С-01', name: 'Запись к терапевту', desc: 'полный автомат: слот → запись (BOOKED)' },
    { n: 2, code: 'С-02', name: 'Запись к неврологу', desc: 'по направлению / без направления (BOOKED_GP)' },
    { n: 3, code: 'С-03', name: 'Перенос записи', desc: 'сначала новая запись, потом отмена старой' },
    { n: 4, code: 'С-04', name: 'Отмена записи', desc: 'подтверждение и отмена (CANCELLED)' },
    { n: 5, code: 'С-05', name: 'Статус своей записи', desc: 'проверка записи, кабинет (INFO_GIVEN)' },
    { n: 6, code: 'С-07', name: 'Справка: график и цены', desc: 'ответ из базы знаний (INFO_GIVEN)' },
    { n: 7, code: 'С-14', name: 'Красный флаг в диалоге', desc: '«давит в груди» → R-01, 103/112, P1' },
    { n: 8, code: 'С-15', name: '«Соедините с человеком»', desc: 'R-02 → перевод с карточкой, P2 (ESCALATED)' },
    { n: 9, code: 'С-06', name: 'Вызов врача на дом', desc: 'сверх скрипта ТЗ: вторник 15:10 (HOME_VISIT)' }
  ];

  var TARGETS = [
    { re: /missed|пропущ/i, target: 'цель ≤ 15%', kind: 'max', limit: 15, share: true },
    { re: /no_show|неявк/i, target: 'цель ≤ 12%', kind: 'max', limit: 12, share: true },
    { re: /autonom|без человека|without_human/i, target: 'цель ≥ 60%', kind: 'min', limit: 60, share: true },
    { re: /wait|ожидан/i, target: 'цель ≤ 30 с', kind: 'max', limit: 30, share: false },
    { re: /duration|длительн/i, target: 'цель ≤ 2,5 мин', kind: 'max', limit: 2.5, share: false }
  ];

  /* ==========================================================================
   * 2. Состояние
   * ======================================================================== */

  var state = {
    sessionId: null,
    localSession: false,
    baseUrl: '',
    ttsEnabled: true,
    demoRunning: false,
    log: [],
    logKeys: {},
    transfer: null,
    queue: [],
    selectedTransferId: null,
    selectedApiId: null,
    currentScenario: '',
    lastTurn: null,
    lastState: null,
    lastSafety: null,
    voice: { asr: false, tts: false, recognition: null, listening: false }
  };

  /* ==========================================================================
   * Утилиты DOM
   * ======================================================================== */

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }

  function emptyNote(container, text) {
    clear(container);
    container.appendChild(el('p', 'empty', text));
  }

  function asText(value) {
    if (value === null || value === undefined || value === '') { return '—'; }
    if (typeof value === 'object') {
      try { return JSON.stringify(value); } catch (e) { return String(value); }
    }
    return String(value);
  }

  function firstDefined(obj, keys) {
    if (!obj || typeof obj !== 'object') { return undefined; }
    for (var i = 0; i < keys.length; i += 1) {
      var k = keys[i];
      if (obj[k] !== undefined && obj[k] !== null && obj[k] !== '') { return obj[k]; }
    }
    return undefined;
  }

  function prettifyKey(key) {
    var s = String(key).replace(/[_.]+/g, ' ').replace(/\s+/g, ' ').trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function keyLabel(key) {
    return SLOT_LABELS[key] || METRIC_LABELS[key] || prettifyKey(key);
  }

  /* Компактное представление значения параметра: слот, запись или список. */
  function compactValue(value, depth) {
    var level = depth || 0;
    if (value === null || value === undefined || value === '') { return '—'; }
    if (typeof value !== 'object') { return String(value); }
    if (Array.isArray(value)) {
      if (!value.length) { return 'нет'; }
      var items = value.slice(0, 4).map(function (v) { return compactValue(v, level + 1); });
      return items.join('; ') + (value.length > 4 ? ' …+' + (value.length - 4) : '');
    }
    if (level > 2) { return JSON.stringify(value); }
    var slotLike = value.doctor_name || value.start || value.time;
    if (slotLike) {
      return [value.date, value.time || (value.start ? String(value.start).slice(-5) : ''),
        value.doctor_name, value.specialty,
        value.cabinet ? ('каб. ' + value.cabinet) : ''].filter(Boolean).join(' · ');
    }
    var visitLike = value.scheduled_at || value.doctor;
    if (visitLike) {
      var doc = value.doctor && typeof value.doctor === 'object'
        ? (value.doctor.full_name || value.doctor.short_name) : value.doctor;
      return ['№' + (value.id !== undefined && value.id !== null ? value.id : '?'),
        value.scheduled_at, doc, value.status].filter(Boolean).join(' · ');
    }
    var text = JSON.stringify(value);
    return text.length > 200 ? (text.slice(0, 200) + '…') : text;
  }

  function formatParams(obj) {
    if (!obj || typeof obj !== 'object') { return asText(obj); }
    var parts = [];
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (v === null || v === undefined || v === '') { return; }
      parts.push(keyLabel(k) + ': ' + compactValue(v));
    });
    return parts.length ? parts.join('; ') : '—';
  }

  /* Время: сервер отдаёт «12.09.2026 10:30» (стенд) или ISO; показываем ЧЧ:ММ:СС */
  function timeOf(ts) {
    if (!ts) {
      var now = new Date();
      return pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' + pad2(now.getSeconds());
    }
    var s = String(ts);
    var m = s.match(/(\d{2}:\d{2}:\d{2})/);
    if (m) { return m[1]; }
    m = s.match(/(\d{2}:\d{2})/);
    if (m) { return m[1] + ':00'; }
    var d = new Date(s);
    if (!isNaN(d.getTime())) {
      return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }
    return s;
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function num(value) {
    var n = Number(value);
    return isNaN(n) ? 0 : n;
  }

  function fmtNum(value) {
    if (typeof value !== 'number' || !isFinite(value)) { return asText(value); }
    if (Math.abs(value) >= 1000) {
      return value.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
    }
    return String(Math.round(value * 100) / 100).replace('.', ',');
  }

  /* ==========================================================================
   * 3. HTTP-слой и журнал «Вызовы API»
   * ======================================================================== */

  function detectBaseUrl() {
    var stored = '';
    try { stored = window.localStorage.getItem('ga-demo-server-base') || ''; } catch (e) { stored = ''; }
    if (location.protocol === 'file:' || location.protocol === '') {
      return (stored || 'http://127.0.0.1:8000').replace(/\/+$/, '');
    }
    return ''; /* тот же origin: только относительные пути */
  }

  function apiUrl(path) {
    var clean = String(path).replace(/^\/+/, '');
    return state.baseUrl ? (state.baseUrl + '/' + clean) : clean;
  }

  function netFetch(path, options) {
    var url = apiUrl(path);
    if (typeof window.fetch === 'function') {
      return window.fetch(url, options);
    }
    /* Очень старый браузер: минимальный XHR-фолбэк, чтобы демо не падало. */
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(options.method || 'GET', url, true);
      if (options.headers) {
        Object.keys(options.headers).forEach(function (h) { xhr.setRequestHeader(h, options.headers[h]); });
      }
      xhr.onload = function () {
        resolve({
          ok: xhr.status >= 200 && xhr.status < 300,
          status: xhr.status,
          text: function () { return Promise.resolve(xhr.responseText); }
        });
      };
      xhr.onerror = function () { reject(new Error('сеть недоступна')); };
      xhr.send(options.body || null);
    });
  }

  /**
   * Один запрос интерфейса к демо-серверу. Каждый вызов попадает в журнал
   * «Вызовы API» (source=UI), чтобы путь/код/длительность были воспроизводимы.
   */
  function request(method, path, body, note, scenarioCode) {
    var startedAt = (window.performance && performance.now) ? performance.now() : Date.now();
    var options = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    var finished = function (status, data, errorText) {
      var endAt = (window.performance && performance.now) ? performance.now() : Date.now();
      pushLog({
        ts: new Date().toISOString(),
        method: method,
        path: '/' + String(path).replace(/^\/+/, ''),
        source: 'UI',
        status: status,
        durationMs: Math.max(0, Math.round(endAt - startedAt)),
        note: note || '',
        scenario: scenarioCode || state.currentScenario || '',
        error: errorText || ''
      });
    };

    return netFetch(path, options).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
        }
        if (!response.ok) {
          var serverMsg = (data && (data.error || data.message)) || ('HTTP ' + response.status);
          finished(response.status, data, String(serverMsg));
          return { ok: false, status: response.status, data: data, error: String(serverMsg) };
        }
        finished(response.status, data, '');
        return { ok: true, status: response.status, data: data, error: '' };
      });
    })['catch'](function (err) {
      finished(0, null, 'нет связи: ' + (err && err.message ? err.message : err));
      return { ok: false, status: 0, data: null, error: 'нет связи: ' + (err && err.message ? err.message : err) };
    });
  }

  /* ------------------------------- журнал ------------------------------- */

  function normSource(src) {
    var s = String(src || '').toUpperCase();
    if (s === 'MOCK' || s === 'MIS' || s === 'KB' || s === 'UI') { return s; }
    return s ? s : 'MIS';
  }

  function pushLog(row) {
    var key = [row.ts || '', row.method || '', row.path || '', row.source || '',
      row.status || 0, row.durationMs || 0, row.note || ''].join('|');
    if (state.logKeys[key]) { return; }
    state.logKeys[key] = true;
    row.time = timeOf(row.ts);
    state.log.push(row);
    if (state.log.length > 600) {
      var dropped = state.log.shift();
      if (dropped) {
        delete state.logKeys([dropped.ts || '', dropped.method || '', dropped.path || '', dropped.source || '',
          dropped.status || 0, dropped.durationMs || 0, dropped.note || ''].join('|'));
      }
    }
    renderLog();
  }

  /* ApiCall[] из ответа /api/turn и /api/session/{id} */
  function ingestActions(actions, fallbackScenario) {
    if (!Array.isArray(actions)) { return 0; }
    var added = 0;
    actions.forEach(function (a) {
      if (!a || typeof a !== 'object') { return; }
      pushLog({
        ts: a.ts || new Date().toISOString(),
        method: a.method || 'GET',
        path: a.path || '',
        source: normSource(a.source),
        status: num(a.status),
        durationMs: num(a.duration_ms !== undefined ? a.duration_ms : a.durationMs),
        note: a.note || a.error || '',
        scenario: a.scenario || fallbackScenario || state.currentScenario || '',
        error: a.error || '',
        ok: a.ok
      });
      added += 1;
    });
    return added;
  }

  function codeClass(status) {
    if (!status) { return 'code-zero'; }
    if (status >= 200 && status < 300) { return 'code-ok'; }
    if (status >= 400 && status < 500) { return 'code-warn'; }
    if (status >= 500) { return 'code-err'; }
    return 'code-zero';
  }

  function renderLog() {
    var tbody = $('api-log');
    if (!tbody) { return; }
    clear(tbody);
    $('api-count').textContent = String(state.log.length);
    if (!state.log.length) {
      var emptyRow = el('tr', 'api-empty');
      var td = el('td', '', 'Журнал пуст. Выполните реплику или сценарий — вызовы появятся здесь.');
      td.colSpan = 7;
      emptyRow.appendChild(td);
      tbody.appendChild(emptyRow);
      return;
    }
    /* Новые сверху: журналируем порядок поступления, показываем обратный. */
    for (var i = state.log.length - 1; i >= 0; i -= 1) {
      var row = state.log[i];
      var src = normSource(row.source);
      var tr = el('tr', 'row-' + src.toLowerCase());
      if (row.status === 0 || row.status >= 400 || row.error) { tr.className += ' row-err'; }

      var tdTime = el('td', 'col-time', row.time);
      tdTime.title = row.ts || '';

      var tdMethod = el('td', 'col-method', row.method || '?');

      var tdPath = el('td', 'col-path', row.path || '—');
      tdPath.title = row.path || '';

      var tdCode = el('td', 'col-code');
      var codeSpan = el('span', codeClass(row.status), row.status ? String(row.status) : '—');
      tdCode.appendChild(codeSpan);

      var msValue = row.durationMs;
      var msText = msValue ? String(msValue) : (row.status ? '<1' : '—');
      var tdMs = el('td', 'col-ms', msText);
      if (!msValue && row.status) { tdMs.title = 'стенд не сообщил длительность: вызов быстрее 1 мс'; }

      var tdSrc = el('td', 'col-src');
      var srcSpan = el('span', 'src src-' + src.toLowerCase(), src);
      srcSpan.title = src === 'MOCK' ? 'Оффлайн-стенд (токена нет или стенд недоступен)'
        : src === 'MIS' ? 'Реальный стенд «Регистратура»'
          : src === 'KB' ? 'Локальная база знаний' : 'Запрос интерфейса к демо-серверу';
      tdSrc.appendChild(srcSpan);

      var tdNote = el('td', 'col-note');
      var noteText = row.note || '';
      if (!noteText && row.error) { noteText = row.error; }
      if (noteText) {
        var noteSpan = el('span', 'note', noteText);
        tdNote.appendChild(noteSpan);
      }
      if (row.scenario) {
        tdNote.appendChild(el('span', 'scenario', row.scenario));
      }
      if (row.error && row.note !== row.error) {
        tdNote.appendChild(el('div', 'hint-small', row.error));
      }
      if (!tdNote.childNodes.length) { tdNote.textContent = '—'; }

      tr.appendChild(tdTime);
      tr.appendChild(tdMethod);
      tr.appendChild(tdPath);
      tr.appendChild(tdCode);
      tr.appendChild(tdMs);
      tr.appendChild(tdSrc);
      tr.appendChild(tdNote);
      tbody.appendChild(tr);
    }
  }

  /* ==========================================================================
   * 4. Панель «Что понял ассистент»
   * ======================================================================== */

  function renderUnderstanding(data) {
    var payload = data || {};
    var st = payload.state || {};
    var safety = payload.safety || null;

    var intentCode = payload.intent || st.intent || '';
    setKv('u-intent', intentCode ? (intentCode + ' — ' + (INTENT_NAMES[intentCode] || 'неизвестный интент')) : '—');

    var code = st.scenario || '';
    setKv('u-scenario', code ? (code + ' — ' + (SCENARIO_NAMES[code] || 'сценарий')) : '— (свободный диалог)');

    var step = st.step || '';
    setKv('u-step', step ? (step + (STEP_NAMES[step] ? ' — ' + STEP_NAMES[step] : '')) : '—');

    var outcome = payload.outcome || st.call_outcome || '';
    setKv('u-outcome', outcome ? (outcome + (OUTCOME_LABELS[outcome] ? ' — ' + OUTCOME_LABELS[outcome] : '')) : '—');

    renderSafety(safety, payload.safetyNote || '');
    renderParams(st);
    renderCounters(st, payload);
  }

  function setKv(id, text) {
    var node = $(id);
    if (node) { node.textContent = text; }
  }

  function renderSafety(safety, note) {
    var box = $('u-safety-box');
    if (!box) { return; }
    box.className = 'safety-box safety-ok';
    if (!safety || typeof safety !== 'object' || !safety.verdict) {
      $('u-safety-verdict').textContent = 'нет данных';
      $('u-safety-verdict').className = 'badge badge-muted';
      $('u-safety-rule').textContent = 'R-—';
      $('u-safety-rule').className = 'badge badge-muted mono';
      $('u-safety-reason').textContent = 'Safety-слой ещё не вызывался: отправьте реплику пациента.';
      $('u-safety-matched').textContent = '';
      return;
    }
    var verdict = String(safety.verdict);
    var ui = SAFETY_UI[verdict] || { label: verdict, cls: 'safety-warn', badge: 'badge-warn', note: '' };
    box.className = 'safety-box ' + ui.cls;
    $('u-safety-verdict').textContent = ui.label;
    $('u-safety-verdict').className = 'badge ' + ui.badge;

    var rule = safety.rule || (payloadRuleFallback(safety));
    $('u-safety-rule').textContent = rule ? String(rule) : 'R-—';
    $('u-safety-rule').className = 'badge mono ' + (rule ? 'badge-warn' : 'badge-muted');

    var reason = safety.reason || ui.note || '';
    if (safety.category) { reason += (reason ? ' · ' : '') + 'группа словаря: ' + safety.category; }
    if (note) { reason += (reason ? ' ' : '') + note; }
    $('u-safety-reason').textContent = reason || 'Пояснение не передано.';
    $('u-safety-matched').textContent = safety.matched ? ('сработало: «' + safety.matched + '»') : '';
  }

  function payloadRuleFallback(safety) {
    var m = String(safety.verdict);
    if (m === 'red_flag_a') { return 'R-01'; }
    if (m === 'red_flag_b') { return 'R-11'; }
    if (m === 'operator') { return 'R-02'; }
    if (m === 'complaint') { return 'R-03'; }
    if (m === 'spd_consent') { return 'R-16'; }
    if (m === 'silence') { return 'R-17'; }
    return '';
  }

  function renderParams(st) {
    var box = $('u-params');
    var slots = st.slots && typeof st.slots === 'object' ? st.slots : {};
    var pending = st.pending && typeof st.pending === 'object' ? st.pending : {};
    var keys = Object.keys(slots);
    var pendKeys = Object.keys(pending);
    $('u-params-count').textContent = String(keys.length + pendKeys.length);

    clear(box);
    if (!keys.length && !pendKeys.length) {
      box.appendChild(el('p', 'empty', 'Параметры появятся по ходу диалога.'));
    } else {
      keys.forEach(function (k) {
        var v = slots[k];
        if (v === null || v === undefined || v === '') { return; }
        var chip = el('span', 'chip');
        chip.title = 'state.slots.' + k;
        chip.appendChild(el('span', 'chip-key', keyLabel(k)));
        chip.appendChild(el('span', 'chip-val', compactValue(v)));
        box.appendChild(chip);
      });
      pendKeys.forEach(function (k) {
        var v = pending[k];
        if (v === null || v === undefined || v === '') { return; }
        if (k === 'greeted_patient' && v) {
          /* Служебное подтверждение приветствия — показываем строкой ниже. */
          return;
        }
        var chip = el('span', 'chip');
        chip.title = 'state.pending.' + k;
        chip.appendChild(el('span', 'chip-key', keyLabel(k)));
        chip.appendChild(el('span', 'chip-val', compactValue(v)));
        box.appendChild(chip);
      });
      if (!box.childNodes.length) {
        box.appendChild(el('p', 'empty', 'Параметры появятся по ходу диалога.'));
      }
    }

    /* Предложенные слоты — кликабельные подсказки для демонстрации */
    var slotBox = $('u-slots');
    var offered = Array.isArray(st.offered_slots) ? st.offered_slots : [];
    $('u-slots-count').textContent = String(offered.length);
    clear(slotBox);
    if (!offered.length) {
      slotBox.appendChild(el('p', 'empty', 'Слоты не предлагались.'));
    } else {
      offered.forEach(function (slot, idx) {
        if (!slot || typeof slot !== 'object') { return; }
        var text = [slot.date, slot.time || (slot.start ? String(slot.start).slice(-5) : ''),
          slot.doctor_name, slot.specialty, slot.cabinet ? ('каб. ' + slot.cabinet) : '']
          .filter(Boolean).join(' · ');
        var chip = el('button', 'chip chip-slot', (idx + 1) + ') ' + (text || asText(slot)));
        chip.type = 'button';
        chip.title = 'Отправить этот вариант в диалог';
        chip.addEventListener('click', function () {
          var answer = slot.time || slot.start || (idx === 0 ? 'первый' : (idx === 1 ? 'второй' : 'третий'));
          sendTurn(String(answer));
        });
        slotBox.appendChild(chip);
      });
    }
  }

  function renderCounters(st, payload) {
    var box = $('u-counters');
    clear(box);
    var counters = [
      { key: 'ходов', val: st.turns !== undefined ? st.turns : '—' },
      { key: 'непониманий', val: st.misunderstanding_count !== undefined ? st.misunderstanding_count : '—' },
      { key: 'тишина', val: st.silence_count !== undefined ? st.silence_count : '—' },
      {
        key: 'запись разговора', val: st.record_opt_out ? 'нет' : (st.recording_consent === false ? 'нет' : 'да'),
        cls: st.record_opt_out || st.recording_consent === false ? 'counter-off' : 'counter-on'
      },
      {
        key: 'звонок', val: st.finished ? 'завершён' : 'идёт',
        cls: st.finished ? 'counter-off' : 'counter-on'
      },
      { key: 'интентов в очереди', val: Array.isArray(st.queue) ? st.queue.length : 0 }
    ];
    counters.forEach(function (c) {
      var card = el('div', 'counter' + (c.cls ? ' ' + c.cls : ''));
      card.appendChild(el('span', 'counter-val', asText(c.val)));
      card.appendChild(el('span', 'counter-key', c.key));
      box.appendChild(card);
    });
    var line = $('u-patient-line');
    if (line) {
      if (st.patient && typeof st.patient === 'object') {
        line.hidden = false;
        line.textContent = 'Пациент идентифицирован: № ' + asText(st.patient.id) + ', ' +
          asText(st.patient.full_name || st.patient.last_name) +
          (st.patient.birth_date ? ', ' + st.patient.birth_date : '') + '.';
      } else if (st.patient) {
        line.hidden = false;
        line.textContent = 'Пациент (как назвал себя): ' + asText(st.patient);
      } else {
        line.hidden = true;
        line.textContent = '';
        line.className = 'hint hint-small';
      }
    }
    if (payload && payload.transfer && payload.transfer.priority) {
      var p = String(payload.transfer.priority);
      var info = PRIORITY_UI[p];
      box.appendChild(prioCard(p, info));
    }
  }

  function prioCard(code, info) {
    var wrap = el('div', 'counter');
    var val = el('span', 'counter-val');
    val.appendChild(el('span', 'prio ' + (info ? info.cls : 'prio-P3'), code));
    wrap.appendChild(val);
    wrap.appendChild(el('span', 'counter-key', info ? info.note : 'приоритет очереди'));
    return wrap;
  }

  /* ==========================================================================
   * 5. Карточка перевода (Приложение В)
   * ======================================================================== */

  function transferField(label, value, wide) {
    var box = el('div', 'transfer-field' + (wide ? ' wide' : ''));
    box.appendChild(el('span', 'tf-label', label));
    var val = el('div', 'tf-value');
    if (value instanceof Node) { val.appendChild(value); } else { val.textContent = asText(value); }
    box.appendChild(val);
    return box;
  }

  function patientText(patient, fallback) {
    if (!patient && !fallback) { return 'не идентифицирован'; }
    var p = patient || {};
    if (typeof p === 'string') { return p; }
    var parts = [];
    if (p.id !== undefined && p.id !== null) { parts.push('№ ' + p.id); }
    var name = p.full_name || [p.last_name, p.first_name, p.middle_name].filter(Boolean).join(' ');
    if (name) { parts.push(name); }
    if (p.birth_date) { parts.push(p.birth_date); }
    var text = parts.join(', ') || asText(p);
    if (fallback && !name) { text += ' · названо: ' + asText(fallback); }
    return text || 'не идентифицирован';
  }

  function renderTransferCard(container, transfer) {
    clear(container);
    if (!transfer || typeof transfer !== 'object') {
      container.appendChild(el('p', 'empty', 'Данных о переводе нет.'));
      return;
    }
    var grid = el('div', 'transfer-grid');

    grid.appendChild(transferField('ID звонка',
      firstDefined(transfer, ['call_id', 'callId', 'transfer_id', 'transferId', 'card_id']) ||
      (transfer.id !== undefined ? transfer.id : 'не передан')));

    var timeStarted = firstDefined(transfer, ['time_started', 'started_at', 'call_started_at', 'created_at']);
    var timeTransferred = firstDefined(transfer, ['time_transferred', 'transferred_at', 'transfer_time', 'at', 'ts', 'time']);
    grid.appendChild(transferField('Время (начало / момент перевода)',
      (timeStarted ? 'начало: ' + asText(timeStarted) : 'начало: —') + '  ·  ' +
      (timeTransferred ? 'перевод: ' + asText(timeTransferred) : 'перевод: —')));

    var status = firstDefined(transfer, ['status', 'state', 'queue_status']);
    grid.appendChild(transferField('Статус перевода', status ? String(status) : 'ожидает оператора'));

    var phone = firstDefined(transfer, ['phone_masked', 'masked_phone', 'phone', 'ani', 'caller', 'number']);
    grid.appendChild(transferField('Номер (маскированный)', phone ? String(phone) : 'не передан'));

    var rawPatient = firstDefined(transfer, ['patient', 'client', 'patient_card']);
    var named = firstDefined(transfer, ['patient_text', 'patient_name', 'called_name', 'said']);
    grid.appendChild(transferField('Пациент', patientText(rawPatient, named), true));

    var intent = firstDefined(transfer, ['intent', 'intent_code', 'intent_name']);
    grid.appendChild(transferField('Интент',
      intent ? (String(intent) + (INTENT_NAMES[String(intent)] ? ' — ' + INTENT_NAMES[String(intent)] : '')) : 'не определён'));

    var params = firstDefined(transfer, ['params', 'slots', 'collected', 'parameters', 'data']);
    grid.appendChild(transferField('Собранные параметры', formatParams(params), true));

    var rule = firstDefined(transfer, ['reason_rule', 'rule', 'reason_code', 'route_rule', 'reason']);
    grid.appendChild(transferField('Причина перевода (R-xx)', rule ? String(rule) : 'не указана'));

    var prio = firstDefined(transfer, ['priority', 'prio', 'prio_code']);
    var prioWrap = el('span', '');
    if (prio && PRIORITY_UI[String(prio)]) {
      prioWrap.appendChild(el('span', 'prio ' + PRIORITY_UI[String(prio)].cls, String(prio)));
      prioWrap.appendChild(el('span', '', ' ' + PRIORITY_UI[String(prio)].note));
    } else {
      prioWrap.textContent = prio ? String(prio) : 'не указан';
    }
    grid.appendChild(transferField('Приоритет', prioWrap));

    var summary = firstDefined(transfer, ['summary', 'resume', 'annotation', 'text', 'reason_text']);
    grid.appendChild(transferField('Резюме', summary ? String(summary) : 'нет резюме', true));

    var dissatisfied = firstDefined(transfer, ['dissatisfaction', 'dissatisfied', 'is_dissatisfied', 'unhappy', 'tone_negative']);
    grid.appendChild(transferField('Признак недовольства',
      dissatisfied === undefined ? 'нет данных' : (dissatisfied ? 'Да' : 'Нет')));

    var consent = firstDefined(transfer, ['recording_consent', 'consent_record', 'consent', 'recording']);
    grid.appendChild(transferField('Согласие на запись',
      consent === undefined ? 'нет данных' : (consent ? 'Да' : 'Нет')));

    var transcript = firstDefined(transfer, ['transcript_url', 'transcript', 'transcript_link']);
    var links = el('div', '');
    if (transcript && typeof transcript === 'string' && transcript.charAt(0) !== '') {
      var a = el('a', '', 'Открыть стенограмму');
      a.href = transcript;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      links.appendChild(a);
    } else {
      links.textContent = consent === false ? 'нет (пациент против записи)' : 'не передана';
    }
    grid.appendChild(transferField('Стенограмма', links));

    var acceptedAt = firstDefined(transfer, ['operator_accepted_at', 'accepted_at']);
    if (acceptedAt) { grid.appendChild(transferField('Принят оператором', acceptedAt)); }
    var opOutcome = firstDefined(transfer, ['operator_outcome', 'outcome']);
    if (opOutcome) {
      grid.appendChild(transferField('Итог оператора',
        String(opOutcome) + (OUTCOME_LABELS[String(opOutcome)] ? ' — ' + OUTCOME_LABELS[String(opOutcome)] : '')));
    }

    container.appendChild(grid);

    var rawBox = $('transfer-raw-box');
    var rawPre = $('transfer-raw');
    if (rawBox && rawPre) {
      rawBox.hidden = false;
      rawPre.textContent = JSON.stringify(transfer, null, 2);
    }
  }

  function applyTransfer(transfer, source) {
    if (!transfer || typeof transfer !== 'object') { return; }
    state.transfer = transfer;
    renderTransferCard($('transfer-body'), transfer);
    if (source) {
      var note = el('p', 'hint hint-small', 'Источник карточки: ' + source);
      $('transfer-body').appendChild(note);
    }
    /* Карточка сразу попадает в очередь оператора, не дожидаясь опроса */
    var localId = firstDefined(transfer, ['call_id', 'transfer_id', 'id', 'card_id']);
    var known = state.queue.some(function (item) {
      return String(firstDefined(item, ['call_id', 'transfer_id', 'id', 'card_id'])) === String(localId);
    });
    if (!known && localId !== undefined) {
      state.queue.unshift(transfer);
      renderQueue();
    }
    /* Сверяем с сервером: в списке /api/transfers у карточек есть служебный index. */
    if (transfer.index === undefined || transfer.index === null) {
      loadQueue();
    }
  }

  /* ==========================================================================
   * 6. Экран оператора
   * ======================================================================== */

  async function loadQueue() {
    var res = await request('GET', 'api/transfers', null, 'Очередь переводов');
    if (!res.ok) { return; }
    var data = res.data;
    var list = [];
    if (Array.isArray(data)) { list = data; }
    else if (data && typeof data === 'object') {
      list = data.transfers || data.queue || data.items || data.cards || [];
      if (!Array.isArray(list)) { list = []; }
    }
    state.queue = list;
    renderQueue();
  }

  function queueId(item) {
    return firstDefined(item, ['call_id', 'transfer_id', 'id', 'card_id', 'key']);
  }

  /* Путь POST /api/transfers/{id}/accept: сервер ждёт index (0-based) из /api/transfers.
     Если index нет (карточка пришла из TurnResult.transfer, а не из очереди) — null:
     кнопки «Принять»/«Итог» блокируем, пока очередь не обновлена с сервера. */
  function queueApiId(item) {
    if (item && typeof item === 'object' && item.index !== undefined && item.index !== null) {
      var n = Number(item.index);
      if (!isNaN(n)) { return n; }
    }
    return null;
  }

  function renderQueue() {
    var box = $('queue');
    $('queue-count').textContent = 'очередь: ' + state.queue.length;
    clear(box);
    if (!state.queue.length) {
      box.appendChild(el('p', 'empty', 'Очередь пуста. Переводы появятся после срабатывания правил R-xx.'));
      return;
    }
    state.queue.forEach(function (item, idx) {
      var id = queueId(item);
      var prio = String(firstDefined(item, ['priority', 'prio', 'prio_code']) || 'P3');
      var btn = el('button', 'queue-item q-' + (PRIORITY_UI[prio] ? prio : 'P3'));
      if (String(id) === String(state.selectedTransferId)) { btn.className += ' selected'; }
      btn.type = 'button';

      var top = el('div', 'q-top');
      var prioInfo = PRIORITY_UI[prio];
      top.appendChild(el('span', 'prio ' + (prioInfo ? prioInfo.cls : 'prio-P3'), prio));
      top.appendChild(el('span', 'q-id', id !== undefined ? String(id) : ('#' + (idx + 1))));
      top.appendChild(el('span', 'q-meta', timeOf(firstDefined(item, ['transferred_at', 'time', 'ts', 'created_at']))));
      btn.appendChild(top);

      var intent = firstDefined(item, ['intent', 'intent_code']);
      var phone = firstDefined(item, ['phone_masked', 'masked_phone', 'phone', 'ani']);
      var meta = [intent ? (String(intent) + (INTENT_NAMES[String(intent)] ? ' ' + INTENT_NAMES[String(intent)] : '')) : '',
        phone ? String(phone) : ''].filter(Boolean).join(' · ');
      if (meta) { btn.appendChild(el('span', 'q-meta', meta)); }

      var summary = firstDefined(item, ['summary', 'resume', 'annotation', 'reason']);
      if (summary) { btn.appendChild(el('span', 'q-sum', String(summary).slice(0, 160))); }

      btn.addEventListener('click', function () { selectTransfer(item); });
      box.appendChild(btn);
    });
  }

  function selectTransfer(item) {
    state.selectedTransferId = queueId(item);
    state.selectedApiId = queueApiId(item);
    renderQueue();
    renderTransferCard($('op-current'), item);
    var id = state.selectedApiId;
    var canAct = (id !== null && id !== undefined);
    $('btn-accept').disabled = !canAct;
    $('btn-outcome').disabled = !canAct;
    if (!canAct) {
      $('op-current').appendChild(el('p', 'hint hint-small toast-err',
        'Карточка получена из TurnResult.transfer и не содержит служебного index. Нажмите «Обновить» в очереди — сервер вернёт список с index, после этого «Принять»/«Итог» станут доступны.'));
    }
  }

  async function acceptTransfer(mode) {
    var id = state.selectedApiId;
    if (id === null || id === undefined) {
      toast('Нет index перевода: сначала обновите очередь («Обновить»).', 'err');
      return;
    }
    var body = { action: 'accept' };
    var note = 'Регистратор принял перевод';
    if (mode === 'outcome') {
      body = { action: 'outcome', outcome: $('op-outcome').value };
      note = 'Итог перевода: ' + body.outcome;
    }
    $('btn-accept').disabled = true;
    $('btn-outcome').disabled = true;
    var res = await request('POST', 'api/transfers/' + encodeURIComponent(id) + '/accept', body, note);
    if (res.ok) {
      toast('Перевод ' + id + ': ' + (mode === 'outcome' ? 'итог зафиксирован' : 'принят') + '.', 'ok');
      var card = res.data && (res.data.transfer || res.data.card || res.data);
      if (card && typeof card === 'object' && (card.summary || card.priority || card.intent)) {
        renderTransferCard($('op-current'), card);
      }
      await loadQueue();
    } else {
      toast('Не удалось выполнить: ' + (res.error || res.status), 'err');
    }
    $('btn-accept').disabled = false;
    $('btn-outcome').disabled = false;
  }

  /* ==========================================================================
   * 7. Метрики и экономика
   * ======================================================================== */

  async function loadMetrics() {
    var res = await request('GET', 'api/metrics', null, 'Метрики и экономика');
    if (!res.ok) {
      $('metrics-source').textContent = 'метрики: ошибка ' + (res.status || 'сети');
      emptyNote($('metrics-body'), 'Не удалось загрузить /api/metrics: ' + (res.error || res.status) +
        '. Формулы расчёта остаются на экране.');
      return;
    }
    $('metrics-source').textContent = 'источник: /api/metrics';
    renderMetrics(res.data);
  }

  function isShareKey(key) {
    if (/(all|total|count|days|всего|дней)$/i.test(key)) { return false; }
    return /(share|rate|доля|percent|pct)/i.test(key);
  }

  function normalizeShare(key, value) {
    if (!isShareKey(key) || typeof value !== 'number') { return value; }
    return value <= 1.0001 ? value * 100 : value;
  }

  function targetHint(key, label, value) {
    var probe = key + ' ' + label;
    for (var i = 0; i < TARGETS.length; i += 1) {
      var t = TARGETS[i];
      if (!t.re.test(probe)) { continue; }
      var v = value;
      if (t.share && typeof v === 'number' && v <= 1.0001) { v = v * 100; }
      var cls = '';
      if (typeof v === 'number') {
        if (t.kind === 'max') { cls = v <= t.limit ? 'm-good' : 'm-bad'; }
        else { cls = v >= t.limit ? 'm-good' : 'm-bad'; }
      }
      return { cls: cls, note: t.target };
    }
    return { cls: '', note: '' };
  }

  function metricCard(key, label, value) {
    var card = el('div', 'metric-card');
    var shown = normalizeShare(key, value);
    var hint = targetHint(key, label, typeof shown === 'number' ? shown : value);
    var isShare = isShareKey(key);
    var text;
    if (typeof shown === 'number') {
      text = isShare ? (fmtNum(shown) + '%') : fmtNum(shown);
    } else {
      text = asText(value);
    }
    card.className = 'metric-card' + (hint.cls ? ' ' + hint.cls : '');
    card.appendChild(el('span', 'm-key', label));
    card.appendChild(el('span', 'm-val', text));
    if (hint.note) { card.appendChild(el('span', 'm-sub', hint.note)); }
    return card;
  }

  function renderMetrics(data) {
    var body = $('metrics-body');
    clear(body);
    var rawBox = $('metrics-raw-box');
    if (rawBox) {
      rawBox.hidden = false;
      $('metrics-raw').textContent = JSON.stringify(data, null, 2);
    }
    if (!data || typeof data !== 'object') {
      body.appendChild(el('p', 'empty', 'Пустой ответ /api/metrics.'));
      return;
    }

    function isPlainObject(v) {
      return v !== null && typeof v === 'object' && !Array.isArray(v);
    }

    /* Массив объектов (месяцы, часы, специальности) — компактная таблица, а не JSON. */
    function objectArrayBlock(label, rows) {
      var block = el('div', 'metric-array');
      block.appendChild(el('div', 'metric-array-title', label + ' · записей: ' + rows.length));
      var cols = [];
      rows.slice(0, 10).forEach(function (row) {
        Object.keys(row).forEach(function (k) {
          if (cols.length < 6 && cols.indexOf(k) === -1 && typeof row[k] !== 'object') { cols.push(k); }
        });
      });
      var table = el('table', 'mini-table');
      var thead = el('thead');
      var headRow = el('tr');
      cols.forEach(function (c) { headRow.appendChild(el('th', '', keyLabel(c))); });
      thead.appendChild(headRow);
      table.appendChild(thead);
      var tbody = el('tbody');
      rows.slice(0, 24).forEach(function (row) {
        var tr = el('tr');
        cols.forEach(function (c) {
          var v = row[c];
          var shown = (typeof v === 'number' && /share|rate|доля/i.test(c))
            ? (fmtNum(v <= 1 ? v * 100 : v) + '%') : fmtNum(v);
          tr.appendChild(el('td', '', shown));
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      var scroll = el('div', 'metric-array-scroll');
      scroll.appendChild(table);
      block.appendChild(scroll);
      if (rows.length > 24) { block.appendChild(el('div', 'metric-note', 'показаны первые 24 из ' + rows.length)); }
      return block;
    }

    function groupNode(title, depth) {
      var group = el('div', 'metric-group');
      group.appendChild(el('h4', '', title));
      var cards = el('div', 'metric-cards');
      group.appendChild(cards);
      return { group: group, cards: cards };
    }

    function walk(obj, group, prefix, depth) {
      var cards = group.cards;
      Object.keys(obj).forEach(function (key) {
        var value = obj[key];
        var path = prefix ? prefix + '.' + key : key;
        var label = METRIC_LABELS[key] || prettifyKey(key);
        if (isPlainObject(value) && /formula|формул/i.test(key)) {
          Object.keys(value).forEach(function (fk) {
            var block = el('div', 'metric-formula');
            block.textContent = prettifyKey(fk) + ': ' + asText(value[fk]);
            cards.appendChild(block);
          });
          return;
        }
        if (isPlainObject(value) && depth < 2) {
          var sub = groupNode(label, depth + 1);
          group.group.appendChild(sub.group);
          walk(value, sub, path, depth + 1);
          return;
        }
        if (Array.isArray(value)) {
          if (!value.length) {
            cards.appendChild(metricCard(key, label, 'нет данных'));
            return;
          }
          if (value.every(function (v) { return typeof v === 'number'; })) {
            var sum = value.reduce(function (a, b) { return a + b; }, 0);
            cards.appendChild(metricCard(path, label + ' (среднее)', sum / value.length));
            cards.appendChild(metricCard(path, label + ' (максимум)', Math.max.apply(null, value)));
            cards.appendChild(metricCard('count', label + ': точек', value.length));
            return;
          }
          if (value.every(function (v) { return isPlainObject(v); })) {
            cards.appendChild(objectArrayBlock(label, value));
            return;
          }
          var listBox = el('div', 'metric-formula');
          listBox.textContent = label + ': ' + value.slice(0, 30).map(String).join(', ') +
            (value.length > 30 ? ' …' : '');
          cards.appendChild(listBox);
          return;
        }
        if (typeof value === 'string' && /(formula|формул|assumption|допущен|note|примеч|method|методика)/i.test(key)) {
          var f = el('div', 'metric-formula');
          f.textContent = label + ': ' + value;
          cards.appendChild(f);
          return;
        }
        if (typeof value === 'boolean') {
          cards.appendChild(metricCard(path, label, value ? 'Да' : 'Нет'));
          return;
        }
        cards.appendChild(metricCard(path, label, value));
      });
    }

    var top = groupNode('Сводка из data/metrics.json', 0);
    body.appendChild(top.group);
    walk(data, top, '', 0);
  }

  /* ==========================================================================
   * 8. Диалог, сценарии, голос
   * ======================================================================== */

  function appendMessage(role, text, meta) {
    var feed = $('dialog-feed');
    if (!feed) { return; }
    var emptyEl = feed.querySelector('.empty');
    if (emptyEl) { feed.removeChild(emptyEl); }

    var map = { patient: 'msg msg-patient', assistant: 'msg msg-assistant', system: 'msg msg-system', safety: 'msg msg-safety' };
    var roleNames = { patient: 'Пациент', assistant: 'Ассистент', system: 'Система', safety: 'Safety-слой' };
    var wrap = el('div', map[role] || 'msg msg-system');
    wrap.appendChild(el('span', 'msg-role', roleNames[role] || role));
    wrap.appendChild(el('div', 'msg-text', text === '' ? '…' : text));
    if (meta) { wrap.appendChild(el('div', 'msg-meta', meta)); }
    feed.appendChild(wrap);
    feed.scrollTop = feed.scrollHeight;
  }

  function appendTurnRaw(payload) {
    var feed = $('dialog-feed');
    if (!feed) { return; }
    var details = el('details', 'msg msg-system');
    details.appendChild(el('summary', '', 'JSON ответа /api/turn (для жюри и отладки)'));
    var pre = el('pre', 'raw-json');
    try { pre.textContent = JSON.stringify(payload, null, 2); } catch (e) { pre.textContent = String(payload); }
    details.appendChild(pre);
    feed.appendChild(details);
    feed.scrollTop = feed.scrollHeight;
  }

  function safetyBanner(safety) {
    if (!safety || !safety.verdict || safety.verdict === 'ok') { return; }
    var ui = SAFETY_UI[safety.verdict] || { label: safety.verdict };
    var parts = ['Сработал safety-слой: ' + ui.label];
    if (safety.rule) { parts.push('правило ' + safety.rule + '.'); }
    if (safety.reason) { parts.push(safety.reason); }
    if (safety.matched) { parts.push('Фрагмент: «' + safety.matched + '».'); }
    appendMessage('safety', parts.join(' '));
  }

  function renderCards(cards) {
    if (!Array.isArray(cards) || !cards.length) { return; }
    cards.forEach(function (card) {
      if (card === null || card === undefined) { return; }
      if (typeof card !== 'object') { appendMessage('system', String(card)); return; }
      var title = firstDefined(card, ['title', 'type', 'name', 'kind', 'code']);
      var lines = [];
      Object.keys(card).forEach(function (k) {
        if (['title', 'type', 'name', 'kind', 'code'].indexOf(k) !== -1) { return; }
        var v = card[k];
        if (v === null || v === undefined || v === '') { return; }
        if (typeof v === 'object') { v = asText(v); }
        lines.push(keyLabel(k) + ': ' + v);
      });
      appendMessage('system', (title ? prettifyKey(title) + ' — ' : '') + (lines.join('; ') || asText(card)));
    });
  }

  function handleTurn(payload, options) {
    var opts = options || {};
    if (!payload || typeof payload !== 'object') { return; }
    state.lastTurn = payload;

    var data = payload.result && typeof payload.result === 'object' ? payload.result : payload;
    var incoming = (data.state && typeof data.state === 'object') ? data.state : null;

    /* На прерывающих ходах (красный флаг, перевод) движок отдаёт state={}:
       сохраняем последнее известное состояние, чтобы панель не пустела. */
    var st;
    if (incoming && Object.keys(incoming).length) {
      st = incoming;
      state.lastState = incoming;
    } else {
      st = state.lastState || {};
    }
    /* safety может прийти null для «чистых» реплик — берём вердикт из state.last_safety,
       а если и его нет (движок вернул state={} на прерывающем ходу) — последний известный. */
    var safety = data.safety || st.last_safety || null;
    var safetyNote = '';
    if (!safety && state.lastSafety) {
      safety = state.lastSafety;
      safetyNote = 'Показан последний известный вердикт: в этом ответе отдельного поля safety нет.';
    }
    if (safety && safety.verdict) { state.lastSafety = safety; }

    if (data.reply) {
      var meta = [];
      if (data.intent || st.intent) { meta.push(data.intent || st.intent); }
      if (data.outcome || st.call_outcome) { meta.push('итог: ' + (data.outcome || st.call_outcome)); }
      appendMessage('assistant', data.reply, meta.join(' · '));
      if (opts.speak !== false) { speak(data.tts_text || data.reply); }
    }

    safetyBanner(safety);
    renderUnderstanding({
      reply: data.reply,
      intent: data.intent || st.intent,
      safety: safety,
      safetyNote: safetyNote,
      state: st,
      outcome: data.outcome || st.call_outcome,
      transfer: data.transfer || st.transfer || null
    });
    ingestActions(data.actions, st.scenario);
    if (data.debug && typeof data.debug === 'object') {
      /* Некоторые сборки кладут вызовы стенда в debug — подхватим и их. */
      ingestActions(data.debug.actions, st.scenario);
    }
    renderCards(data.cards);

    var transfer = data.transfer || st.transfer || null;
    if (transfer) { applyTransfer(transfer, '/api/turn → transfer'); }

    var outcomeText = String(data.outcome || '');
    var endedThisTurn = !!transfer || !!outcomeText.match(/^(EMERGENCY|ESCALATED|COMPLAINT)$/) ||
      !!(incoming && incoming.finished);
    if (endedThisTurn) {
      appendMessage('system', 'Звонок завершён. Код результата: ' +
        asText(data.outcome || st.call_outcome || 'DROPPED') + '.');
    }
    if (opts.raw) { appendTurnRaw(data); }
  }

  function updateSessionBadge() {
    var badge = $('badge-session');
    if (!badge) { return; }
    badge.textContent = 'сессия: ' + (state.sessionId ? state.sessionId : '—');
    badge.title = state.localSession ? 'ID локальный: демо-сервер не ответил, интерфейс работает автономно' : 'POST /api/session';
    badge.className = 'badge ' + (state.localSession ? 'badge-err' : 'badge-muted');
  }

  async function ensureSession() {
    if (state.sessionId && !state.localSession) { return state.sessionId; }
    var res = await request('POST', 'api/session', {}, 'Создание сессии диалога');
    if (res.ok && res.data && res.data.session_id) {
      state.sessionId = String(res.data.session_id);
      state.localSession = false;
      updateSessionBadge();
      if (res.data.greeting) {
        appendMessage('assistant', res.data.greeting, 'приветствие сценария');
      }
      return state.sessionId;
    }
    state.sessionId = state.sessionId || ('local-' + Date.now());
    state.localSession = true;
    updateSessionBadge();
    toast('Демо-сервер недоступен: интерфейс работает в автономном режиме (' + (res.error || res.status) + ').', 'err');
    return state.sessionId;
  }

  async function sendTurn(text) {
    var value = String(text || '').trim();
    if (!value) { return; }
    if (state.demoRunning) { toast('Идёт прогон демо-сценария, подождите.', 'err'); return; }
    appendMessage('patient', value);
    var sessionId = await ensureSession();
    var res = await request('POST', 'api/turn', { session_id: sessionId, text: value }, 'Реплика пациента: ход диалога');
    if (!res.ok) {
      appendMessage('system', 'Не удалось получить ответ /api/turn: ' + (res.error || res.status) + '.');
      return;
    }
    if (res.data && res.data.session_id && res.data.session_id !== state.sessionId) {
      state.sessionId = String(res.data.session_id);
      state.localSession = false;
      updateSessionBadge();
    }
    handleTurn(res.data, { speak: true });
    /* Если ход вызвал обращения к стенду — обновляем счётчик журнала стенда в шапке. */
    if (res.data && Array.isArray(res.data.actions) && res.data.actions.length) {
      refreshHealth();
    }
  }

  async function syncSession() {
    var sessionId = await ensureSession();
    var res = await request('GET', 'api/session/' + encodeURIComponent(sessionId), null, 'Состояние диалога и журнал стенда');
    if (!res.ok || !res.data) { return; }
    var data = res.data;
    ingestActions(data.actions, data.session && data.session.scenario);
    if (data.session && !state.lastTurn) {
      /* Панель «Что понял ассистент» отражает последний ход диалога (/api/turn),
         поэтому состояние сессии используем только если ходов ещё не было. */
      renderUnderstanding({
        state: data.session,
        intent: data.session.intent,
        safety: data.session.last_safety || null,
        outcome: data.session.call_outcome || data.outcome
      });
    }
    var transfers = data.transfers;
    if (Array.isArray(transfers) && transfers.length) {
      state.queue = transfers;
      renderQueue();
      applyTransfer(transfers[transfers.length - 1], '/api/session/{id} → transfers');
    } else if (data.transfer) {
      applyTransfer(data.transfer, '/api/session/{id} → transfer');
    } else if (data.session && data.session.transfer) {
      applyTransfer(data.session.transfer, 'DialogState.transfer');
    }
  }

  /* Шаг демо-сценария: {"turn":n,"role":"patient","text":...} либо
     {"turn":n,"role":"assistant","reply":...,"intent":...,"state":...,"actions":[...]}. */
  function renderStep(step, index) {
    if (step === null || step === undefined) { return; }
    if (typeof step === 'string') { appendMessage('patient', step); return; }
    if (typeof step !== 'object') { appendMessage('system', String(step)); return; }

    var kind = String(step.role || step.type || '').toLowerCase();
    var data = (step.result && typeof step.result === 'object') ? step.result
      : (step.response && typeof step.response === 'object') ? step.response
        : (step.turn && typeof step.turn === 'object') ? step.turn : null;

    var patientText = firstDefined(step, ['text', 'patient', 'input', 'utterance', 'said']);
    if (patientText && typeof patientText === 'object') { patientText = asText(patientText); }

    if (kind === 'system') {
      if (step.text) { appendMessage('system', String(step.text)); }
      if (replyOf(step, data)) { handleTurn(data || step, { speak: false }); }
      return;
    }

    var isAssistant = (kind === 'assistant' || kind === 'agent' || kind === 'bot');
    var reply = replyOf(step, data, isAssistant);

    if (!isAssistant && patientText) { appendMessage('patient', String(patientText)); }

    if (reply || data) {
      handleTurn(data || step, { speak: false });
      return;
    }
    if (isAssistant && step.text) { appendMessage('assistant', String(step.text)); return; }
    if (step.text && !patientText) { appendMessage('system', String(step.text)); return; }
    if (!patientText) {
      appendMessage('system', 'Шаг ' + (index + 1) + ': ' + asText(step));
    }
  }

  function replyOf(step, data, isAssistant) {
    return step.reply || (data && data.reply) || (isAssistant ? step.text : '');
  }

  async function runDemo(n, code, name) {
    if (state.demoRunning) { return; }
    state.demoRunning = true;
    state.currentScenario = code || '';
    setScenarioButtonsEnabled(false);
    appendMessage('system', 'Демо-сценарий ' + (code ? code + ' «' + name + '»' : ('№ ' + n)) + ': запускаю POST /api/demo/' + n + '…');

    var res = await request('POST', 'api/demo/' + n, {}, 'Прогон демо-сценария: ' + (code || n) + ' ' + (name || ''), code);
    if (!res.ok) {
      appendMessage('system', 'Сценарий не запустился: ' + (res.error || res.status) + '.');
      toast('Ошибка запуска сценария ' + n + ': ' + (res.error || res.status), 'err');
      state.demoRunning = false;
      setScenarioButtonsEnabled(true);
      return;
    }

    var data = res.data || {};
    if (data.session_id) {
      state.sessionId = String(data.session_id);
      state.localSession = false;
      updateSessionBadge();
    }
    var steps = Array.isArray(data) ? data : (data.steps || data.turns || []);
    if (!Array.isArray(steps) || !steps.length) {
      appendMessage('system', 'Сервер не вернул шаги (steps). Смотрите JSON в панели ниже.');
      appendTurnRaw(data);
    } else {
      /* Демо-скрипт проигрывается целиком, со своим приветствием: чистим ленту. */
      clear($('dialog-feed'));
      appendMessage('system', 'Демо-сценарий ' + (data.title || (code || ('№ ' + n))) +
        ' · шагов: ' + steps.length + ' · сессия: ' + (data.session_id || state.sessionId || '—'));
      steps.forEach(function (step, idx) { renderStep(step, idx); });
    }
    if (data.transfer) { applyTransfer(data.transfer, '/api/demo/' + n + ' → transfer'); }
    if (data.outcome) { appendMessage('system', 'Код результата сценария: ' + data.outcome); }

    await syncSession();
    await refreshHealth();
    if (state.log.length) {
      var last = state.log[state.log.length - 1];
      toast('Сценарий ' + (code || n) + ' выполнен. Последний вызов: ' + last.method + ' ' + last.path + ' → ' + (last.status || '—') + '.', 'ok');
    }
    state.demoRunning = false;
    setScenarioButtonsEnabled(true);
  }

  function setScenarioButtonsEnabled(enabled) {
    var buttons = document.querySelectorAll('.scenario-btn');
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].disabled = !enabled;
    }
    var reset = $('btn-reset');
    if (reset) { reset.disabled = !enabled; }
  }

  async function resetDemo() {
    if (state.demoRunning) { return; }
    var resetData = null;
    if (state.sessionId && !state.localSession) {
      var resetRes = await request('POST', 'api/session/' + encodeURIComponent(state.sessionId) + '/reset', {}, 'Сброс сессии');
      if (resetRes.ok && resetRes.data) { resetData = resetRes.data; }
    }
    state.sessionId = null;
    state.localSession = false;
    state.currentScenario = '';
    state.transfer = null;
    state.lastTurn = null;
    state.lastState = null;
    state.queue = [];
    state.selectedTransferId = null;
    state.selectedApiId = null;
    state.log = [];
    state.logKeys = {};
    state.lastSafety = null;
    renderLog();

    emptyNote($('dialog-feed'), 'Демо сброшено. Начните разговор: наберите реплику пациента или включите микрофон.');
    renderSafety(null);
    setKv('u-intent', '—');
    setKv('u-scenario', '—');
    setKv('u-step', '—');
    setKv('u-outcome', '—');
    emptyNote($('u-params'), 'Параметры появятся по ходу диалога.');
    emptyNote($('u-slots'), 'Слоты не предлагались.');
    renderCounters({}, {});
    $('u-params-count').textContent = '0';
    $('u-slots-count').textContent = '0';
    emptyNote($('transfer-body'), 'Переводов пока не было.');
    var rawBox = $('transfer-raw-box');
    if (rawBox) { rawBox.hidden = true; }
    emptyNote($('op-current'), 'Выберите перевод в очереди слева.');
    $('btn-accept').disabled = true;
    $('btn-outcome').disabled = true;
    renderQueue();

    if (resetData && resetData.session_id) {
      state.sessionId = String(resetData.session_id);
      state.localSession = false;
      updateSessionBadge();
      if (resetData.greeting) { appendMessage('assistant', resetData.greeting, 'приветствие сценария'); }
    } else {
      await ensureSession();
    }
    await loadQueue();
    toast('Демо сброшено, создана новая сессия.', 'ok');
  }

  async function refreshHealth() {
    var badge = $('badge-stand');
    var res = await request('GET', 'api/health', null, 'Состояние стенда');
    if (!res.ok || !res.data || typeof res.data !== 'object') {
      badge.textContent = 'стенд: нет связи';
      badge.className = 'badge badge-err';
      badge.title = res.error || ('HTTP ' + res.status);
      $('badge-token').textContent = 'токен: —';
      $('badge-token').className = 'badge badge-muted';
      $('badge-doctors').textContent = 'справочник: —';
      $('badge-doctors').className = 'badge badge-muted';
      return;
    }
    var d = res.data;
    var offline = d.offline === true;
    badge.textContent = offline ? 'стенд: ОФФЛАЙН (MOCK)' : 'стенд: реальный (MIS)';
    badge.className = 'badge ' + (offline ? 'badge-mock' : 'badge-mis');
    badge.title = 'GET /api/health → ok=' + asText(d.ok) + ', offline=' + asText(d.offline);

    var tokenSet = d.token_set === true;
    $('badge-token').textContent = 'токен: ' + (tokenSet ? 'есть' : 'нет');
    $('badge-token').className = 'badge ' + (tokenSet ? 'badge-mis' : 'badge-mock');

    var doctors = d.doctors !== undefined && d.doctors !== null ? d.doctors : '—';
    var horizon = d.horizon_days !== undefined && d.horizon_days !== null ? d.horizon_days : '—';
    var parts = ['врачи: ' + doctors, 'горизонт: ' + horizon + ' дн.'];
    if (d.mis_calls !== undefined && d.mis_calls !== null) {
      parts.push('журнал стенда: ' + d.mis_calls);
    }
    if (d.slot_engine_ready === false) { parts.push('сетка слотов не собрана'); }
    $('badge-doctors').textContent = parts.join(' · ');
    $('badge-doctors').className = 'badge ' + (d.slot_engine_ready === false ? 'badge-warn' : 'badge-muted');
    $('badge-doctors').title = 'GET /api/health → mis_calls=' + asText(d.mis_calls) +
      ', slot_engine_ready=' + asText(d.slot_engine_ready) +
      (d.server_time ? ', время сервера ' + d.server_time : '') +
      (d.base_url ? ' · ' + d.base_url : '');
  }

  /* --------------------------------- голос -------------------------------- */

  function setupVoice() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    state.voice.asr = !!SR;
    state.voice.tts = typeof window.speechSynthesis !== 'undefined' && typeof window.SpeechSynthesisUtterance !== 'undefined';

    var notice = $('voice-status');
    var problems = [];
    if (!state.voice.asr) {
      problems.push('распознавание речи (SpeechRecognition) недоступно');
    }
    if (!state.voice.tts) {
      problems.push('синтез речи (speechSynthesis) недоступен');
    }

    if (!problems.length) {
      notice.className = 'notice notice-info';
      notice.textContent = 'Голос доступен: распознавание ru-RU и озвучивание ответов. Демо полностью работает и в текстовом режиме.';
      $('dialog-mode').textContent = 'режим: голос + текст';
      $('dialog-mode').className = 'badge badge-ok';
    } else {
      notice.className = 'notice notice-warn';
      notice.textContent = 'Голосовой режим ограничен: ' + problems.join('; ') +
        '. Если страница открыта как file:// — микрофон в Chrome недоступен: откройте http://127.0.0.1:8000 (там он работает). ' +
        'Текстовый режим сохраняет полную работоспособность: сценарии, панели и вызовы API работают так же.';
      $('dialog-mode').textContent = 'режим: только текст';
      $('dialog-mode').className = 'badge badge-warn';
      var mic = $('btn-mic');
      mic.disabled = true;
      mic.title = 'Web Speech API недоступен в этом браузере';
      $('mic-label').textContent = 'Микрофон недоступен';
    }
    if (!state.voice.tts) {
      state.ttsEnabled = false;
      var tts = $('btn-tts');
      tts.textContent = '🔇 Озвучивание недоступно';
      tts.setAttribute('aria-pressed', 'false');
      tts.disabled = true;
    }
  }

  function toggleMic() {
    if (!state.voice.asr) { return; }
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var mic = $('btn-mic');
    if (state.voice.listening && state.voice.recognition) {
      try { state.voice.recognition.stop(); } catch (e) { /* уже остановлен */ }
      return;
    }
    var recognition = new SR();
    recognition.lang = 'ru-RU';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    state.voice.recognition = recognition;

    recognition.onstart = function () {
      state.voice.listening = true;
      mic.className = 'btn btn-mic listening';
      $('mic-label').textContent = 'Слушаю…';
      $('voice-status').className = 'notice notice-info';
      $('voice-status').textContent = 'Слушаю реплику пациента (ru-RU). Говорите обычным голосом.';
    };
    recognition.onresult = function (event) {
      var interim = '';
      var finalText = '';
      for (var i = event.resultIndex; i < event.results.length; i += 1) {
        var res = event.results[i];
        if (res.isFinal) { finalText += res[0].transcript; } else { interim += res[0].transcript; }
      }
      if (interim) { $('dialog-input').value = interim; }
      if (finalText) {
        $('dialog-input').value = '';
        sendTurn(finalText.trim());
        $('voice-status').className = 'notice notice-info';
        $('voice-status').textContent = 'Распознано: «' + finalText.trim() + '». Голос доступен, демо работает и в текстовом режиме.';
      }
    };
    recognition.onerror = function (event) {
      var reasons = {
        'not-allowed': 'нет разрешения на микрофон: разрешите доступ в настройках браузера',
        'service-not-allowed': 'браузер запретил сервис распознавания (нужен https или localhost)',
        'no-speech': 'речь не распознана, попробуйте ещё раз',
        'audio-capture': 'микрофон не найден или занят',
        'network': 'распознавание недоступно без сети — используйте текстовый ввод',
        'aborted': 'запись остановлена'
      };
      var reason = reasons[event.error] || ('ошибка распознавания: ' + event.error);
      $('voice-status').className = 'notice notice-warn';
      $('voice-status').textContent = 'Голосовой ввод не сработал (' + reason + '). Продолжайте текстом — работоспособность демо сохраняется.';
    };
    recognition.onend = function () {
      state.voice.listening = false;
      mic.className = 'btn btn-mic';
      $('mic-label').textContent = 'Микрофон';
      state.voice.recognition = null;
    };
    try {
      recognition.start();
    } catch (e) {
      $('voice-status').className = 'notice notice-warn';
      $('voice-status').textContent = 'Не удалось запустить распознавание: ' + (e && e.message ? e.message : e) + '. Используйте текстовый ввод.';
    }
  }

  function pickRuVoice() {
    if (!state.voice.tts) { return null; }
    var voices = window.speechSynthesis.getVoices() || [];
    for (var i = 0; i < voices.length; i += 1) {
      if (/^ru/i.test(voices[i].lang || '')) { return voices[i]; }
    }
    return null;
  }

  function speak(text) {
    if (!state.ttsEnabled || !state.voice.tts || !text) { return; }
    try {
      window.speechSynthesis.cancel();
      var utterance = new window.SpeechSynthesisUtterance(String(text).slice(0, 600));
      utterance.lang = 'ru-RU';
      utterance.rate = 1.0;
      utterance.pitch = 1.0;
      var voice = pickRuVoice();
      if (voice) { utterance.voice = voice; }
      window.speechSynthesis.speak(utterance);
    } catch (e) {
      /* Синтез не критичен: текстовый режим остаётся рабочим. */
    }
  }

  function toggleTts() {
    if (!state.voice.tts) { return; }
    state.ttsEnabled = !state.ttsEnabled;
    var btn = $('btn-tts');
    btn.textContent = state.ttsEnabled ? '🔊 Озвучивание: вкл' : '🔇 Озвучивание: выкл';
    btn.setAttribute('aria-pressed', state.ttsEnabled ? 'true' : 'false');
    if (!state.ttsEnabled) {
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    }
  }

  /* ------------------------------- прочее UI ------------------------------ */

  var toastTimer = null;
  function toast(text, kind) {
    var node = $('toast');
    if (!node) { return; }
    node.textContent = text;
    node.className = 'toast' + (kind === 'err' ? ' toast-err' : (kind === 'ok' ? ' toast-ok' : ''));
    if (toastTimer) { window.clearTimeout(toastTimer); }
    toastTimer = window.setTimeout(function () {
      node.textContent = '';
      node.className = 'toast';
    }, 12000);
  }

  function buildScenarioButtons() {
    var box = $('scenario-buttons');
    clear(box);
    DEMO_SCENARIOS.forEach(function (sc) {
      var btn = el('button', 'scenario-btn');
      btn.type = 'button';
      btn.title = 'POST /api/demo/' + sc.n + ' — ' + sc.code + ' ' + sc.name;
      btn.appendChild(el('span', 'sc-num', '№ ' + sc.n + ' · ' + sc.code));
      btn.appendChild(el('span', 'sc-name', sc.name));
      btn.appendChild(el('span', 'sc-desc', sc.desc));
      btn.addEventListener('click', function () { runDemo(sc.n, sc.code, sc.name); });
      box.appendChild(btn);
    });
  }

  function buildOutcomeSelect() {
    var select = $('op-outcome');
    clear(select);
    OUTCOMES.forEach(function (pair) {
      var option = el('option', '', pair[0] + ' — ' + pair[1]);
      option.value = pair[0];
      select.appendChild(option);
    });
    select.value = 'ESCALATED';
  }

  function applyBaseUrlInput() {
    var input = $('input-base');
    if (!input) { return; }
    var hint = $('base-hint');
    var isFile = (location.protocol === 'file:' || location.protocol === '');
    input.value = state.baseUrl || (isFile ? 'http://127.0.0.1:8000' : location.origin);
    input.disabled = !isFile;
    input.title = isFile
      ? 'Страница открыта как file:// — укажите адрес демо-сервера (по умолчанию http://127.0.0.1:8000)'
      : 'Страница открыта с демо-сервера: используются относительные пути /api/…';
    if (hint) {
      hint.textContent = isFile
        ? 'Открыто как file:// — годится только для текстового режима. Откройте http://127.0.0.1:8000 — там доступен микрофон (localhost считается защищённым контекстом).'
        : 'Страница открыта с демо-сервера: микрофон доступен (localhost — защищённый контекст), запросы идут относительными путями /api/…';
    }
  }

  function bindEvents() {
    $('dialog-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var input = $('dialog-input');
      var value = input.value;
      input.value = '';
      sendTurn(value);
    });
    $('btn-mic').addEventListener('click', toggleMic);
    $('btn-tts').addEventListener('click', toggleTts);
    $('btn-reset').addEventListener('click', resetDemo);
    $('btn-health').addEventListener('click', refreshHealth);
    $('btn-log-clear').addEventListener('click', function () {
      state.log = [];
      state.logKeys = {};
      renderLog();
      toast('Журнал интерфейса очищен. Следующие вызовы снова появятся здесь.', 'ok');
    });
    $('btn-queue-refresh').addEventListener('click', loadQueue);
    $('btn-metrics-refresh').addEventListener('click', loadMetrics);
    $('btn-accept').addEventListener('click', function () { acceptTransfer('accept'); });
    $('btn-outcome').addEventListener('click', function () { acceptTransfer('outcome'); });
    $('input-base').addEventListener('change', function (event) {
      var value = String(event.target.value || '').trim().replace(/\/+$/, '');
      state.baseUrl = value;
      try { window.localStorage.setItem('ga-demo-server-base', value); } catch (e) { /* ignore */ }
      state.sessionId = null;
      state.localSession = false;
      updateSessionBadge();
      toast('Адрес сервера обновлён: ' + (value || location.origin), 'ok');
      refreshHealth();
      ensureSession();
    });
  }

  function init() {
    state.baseUrl = detectBaseUrl();
    applyBaseUrlInput();
    buildScenarioButtons();
    buildOutcomeSelect();
    bindEvents();
    renderLog();
    renderSafety(null);
    renderCounters({}, {});
    setupVoice();
    refreshHealth();
    /* Первичные загрузки — последовательно, чтобы не устраивать залп запросов. */
    ensureSession().then(function () {
      loadQueue();
      loadMetrics();
    });
    /* Периодический опрос состояния стенда, чтобы индикатор не «залипал». */
    window.setInterval(function () {
      if (!state.demoRunning) { refreshHealth(); }
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
