/* =============================================================================
 * Аккордеон на нативных <details>/<summary> — контейнерный паттерн Shadcn
 * Accordion без React, Radix, Tailwind и сборки.
 *
 * Контракт разметки (согласован, менять нельзя):
 *   <details class="acc" data-slot="accordion-item" open>
 *     <summary class="acc-trigger" data-slot="accordion-trigger">
 *       <span class="acc-label">Демо-сценарии</span>
 *       <span class="acc-count" data-acc-count>9</span>
 *       <svg class="acc-chevron" viewBox="0 0 16 16" aria-hidden="true">…</svg>
 *     </summary>
 *     <div class="acc-content" data-slot="accordion-content">…любое содержимое…</div>
 *   </details>
 *
 * Почему выбран нативный details/summary:
 *   браузер сам даёт семантику раскрывающегося блока, aria-expanded на summary,
 *   свойство open, фокус по Tab и переключение по Enter/Space. Поэтому компонент
 *   НЕ ставит role и aria-expanded руками (получился бы дубль в дереве
 *   доступности) и НЕ перехватывает клавиатуру. Его зона ответственности:
 *   data-state для CSS и тестов, режим «открыт только один», персистентность
 *   в localStorage, счётчик в шапке и класс анимации раскрытия.
 *
 * Публичный API: window.Accordion (expand, collapse, toggle, openAll, closeAll,
 * refresh, getState) — см. блок 7. Внешних зависимостей и сетевых запросов нет,
 * синтаксис ES5, как в app.js.
 * ========================================================================== */
'use strict';

(function () {
  /* ==========================================================================
   * 1. Константы и помощники
   * ======================================================================== */

  var ITEM_SELECTOR = 'details.acc';        /* корень одного пункта аккордеона */
  var CONTENT_SELECTOR = '.acc-content';    /* раскрываемое содержимое */
  var COUNT_SELECTOR = '[data-acc-count]';  /* счётчик в summary */
  var SOURCE_SELECTOR = '[data-acc-source]';/* источник числа для счётчика */
  var PERSIST_PREFIX = 'acc:';              /* ключ localStorage: 'acc:' + data-persist */
  var SINGLE_ATTR = 'data-accordion-single';/* «открыт только один» */
  var ANIM_CLASS = 'acc-anim';              /* вешается на details на время перехода */
  var ANIM_FALLBACK_MS = 600;               /* страховка, если transitionend не придёт */

  /* Ссылка на наблюдатель держится, чтобы он не был собран сборщиком мусора. */
  var observer = null;

  function toElement(target) {
    if (!target) { return null; }
    if (typeof target === 'string') {
      try {
        return document.querySelector(target);
      } catch (e) {
        console.error('Accordion: некорректный селектор «' + target + '»', e);
        return null;
      }
    }
    return target.nodeType === 1 ? target : null;
  }

  /* Приводим цель к конкретному <details>: строку — через querySelector,
     вложенный узел (например, клик по svg-шеврону) — через ближайший details. */
  function itemFor(target) {
    var el = toElement(target);
    if (!el) { return null; }
    if (el.tagName === 'DETAILS') { return el; }
    return el.closest ? el.closest('details') : null;
  }

  function triggerOf(item) {
    return item.querySelector('summary.acc-trigger') || item.querySelector('summary');
  }

  function contentOf(item) {
    return item.querySelector(CONTENT_SELECTOR);
  }

  /* ==========================================================================
   * 2. Персистентность: data-persist → localStorage['acc:' + значение]
   *
   * Состояние пишется при каждом изменении (клик мышью, Enter/Space, вызов
   * API) и читается один раз при инициализации пункта. Ошибки хранилища
   * намеренно глушим: приватный режим или запрет cookies не должны ломать
   * аккордеон — просто состояние не запомнится.
   * ======================================================================== */

  function persistKeyOf(item) {
    var raw = item.getAttribute('data-persist');
    return raw === null ? null : PERSIST_PREFIX + raw;
  }

  function storageRead(key) {
    try {
      var value = window.localStorage.getItem(key);
      return value === 'open' || value === 'closed' ? value : null;
    } catch (e) {
      return null;
    }
  }

  function storageWrite(key, value) {
    try {
      if (window.localStorage.getItem(key) !== value) {
        window.localStorage.setItem(key, value);
      }
    } catch (e) {
      /* Хранилище недоступно — молча пропускаем. */
    }
  }

  /* ==========================================================================
   * 3. Режим «открыт только один» (data-accordion-single)
   * ======================================================================== */

  /* Ближайший контейнер с data-accordion-single. Если атрибут стоит на самом
     details, группой считаем его соседей по родителю — так работают обе
     разметки: и обёртка <div data-accordion-single>, и пометка на каждом
     details. */
  function singleContainerOf(item) {
    var node = item.parentNode;
    while (node && node.nodeType === 1) {
      if (node.hasAttribute && node.hasAttribute(SINGLE_ATTR)) { return node; }
      node = node.parentNode;
    }
    if (item.hasAttribute && item.hasAttribute(SINGLE_ATTR)) {
      return item.parentNode && item.parentNode.nodeType === 1 ? item.parentNode : null;
    }
    return null;
  }

  /* Вложенный аккордеон (details.acc внутри .acc-content другого пункта) в
     группу внешнего контейнера не входит. */
  function isNested(item, container) {
    var node = item.parentNode;
    while (node && node !== container) {
      if (node.nodeType === 1 && node.tagName === 'DETAILS' && node.classList.contains('acc')) {
        return true;
      }
      node = node.parentNode;
    }
    return false;
  }

  function siblingsOf(item) {
    var container = singleContainerOf(item);
    if (!container) { return []; }
    var all = container.querySelectorAll(ITEM_SELECTOR);
    var out = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i] !== item && !isNested(all[i], container)) { out.push(all[i]); }
    }
    return out;
  }

  /* ==========================================================================
   * 4. Состояние, персистентность, анимация
   * ======================================================================== */

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function stopAnimation(item) {
    if (item._accAnimTimer) {
      window.clearTimeout(item._accAnimTimer);
      item._accAnimTimer = null;
    }
    if (item.classList.contains(ANIM_CLASS)) { item.classList.remove(ANIM_CLASS); }
  }

  /* .acc-anim висит на details, пока идёт переход: CSS (его ведёт Lead)
     использует класс, чтобы анимировать высоту .acc-content. Анимация не
     навязывается при reduced-motion — там класс не нужен вовсе. */
  function startAnimation(item) {
    if (prefersReducedMotion()) { return; }
    item.classList.add(ANIM_CLASS);
    if (item._accAnimTimer) { window.clearTimeout(item._accAnimTimer); }
    item._accAnimTimer = window.setTimeout(function () { stopAnimation(item); }, ANIM_FALLBACK_MS);
  }

  /* data-state — единственная ручная правка атрибутов: её ждут CSS (стрелка,
     высота) и тесты. role/aria-expanded не трогаем: их даёт нативный details.
     Наблюдатель слушает только childList, поэтому наши data-state и acc-anim
     не порождают мутаций и не зацикливают refresh(). */
  function syncState(item, animate) {
    var next = item.open ? 'open' : 'closed';
    if (item.getAttribute('data-state') !== next) {
      item.setAttribute('data-state', next);
      if (animate) { startAnimation(item); }
    }
    var key = persistKeyOf(item);
    if (key) { storageWrite(key, next); }
  }

  function closeSiblings(item, animate) {
    var list = siblingsOf(item);
    for (var i = 0; i < list.length; i++) {
      if (list[i].open) { list[i].open = false; }
      syncState(list[i], animate);
    }
  }

  function setOpen(item, open, animate) {
    if (open) {
      if (singleContainerOf(item)) { closeSiblings(item, animate); }
      if (!item.open) { item.open = true; }
    } else if (item.open) {
      item.open = false;
    }
    syncState(item, animate);
    return item;
  }

  function enforceSingle(item, animate) {
    if (item.open && singleContainerOf(item)) { closeSiblings(item, animate); }
  }

  /* Навешиваем обработчики один раз на пункт. Событие toggle приходит и от
     мыши, и от Enter/Space — собственную клавиатурную логику не пишем.
     Клик по summary оставлен второй страховкой для браузеров без toggle:
     preventDefault не вызывается, нативное поведение не переопределяется. */
  function ensureItem(item) {
    if (item._accReady) { return item; }
    item._accReady = true;
    var trigger = triggerOf(item);
    if (trigger) {
      trigger.addEventListener('click', function () {
        window.setTimeout(function () { handleToggle(item); }, 0);
      });
    }
    item.addEventListener('toggle', function () { handleToggle(item); });
    item.addEventListener('transitionend', function (event) {
      if (event.target === item || event.target === contentOf(item)) { stopAnimation(item); }
    });
    return item;
  }

  function handleToggle(item) {
    if (item.open) { closeSiblings(item, true); }
    syncState(item, true);
  }

  function setUpItem(item) {
    if (!item._accReady) {
      ensureItem(item);
      var key = persistKeyOf(item);
      var stored = key ? storageRead(key) : null;
      if (stored) {
        setOpen(item, stored === 'open', false); /* восстановление без анимации */
      } else {
        syncState(item, false);                  /* data-state по разметке (open в HTML) */
      }
      /* В группе «один открытый» несколько open в разметке оставляем одним:
         побеждает последний открытый пункт по порядку документа. */
      enforceSingle(item, false);
    } else {
      syncState(item, false);
    }
  }

  /* Переинициализация: подхватывает новые .acc, снимает расхождения
     data-state, восстанавливает персистентность у новых пунктов и обновляет
     счётчики. Идемпотентна — повторный вызов ничего не меняет в DOM. */
  function refresh() {
    var items = document.querySelectorAll(ITEM_SELECTOR);
    for (var i = 0; i < items.length; i++) {
      setUpItem(items[i]);
      updateCounter(items[i]);
    }
    return items.length;
  }

  /* ==========================================================================
   * 5. Счётчик в шапке пункта
   *
   * Если в summary есть [data-acc-count], а в .acc-content — [data-acc-source],
   * текст счётчика берётся из атрибута data-count-value источника (это число
   * фиксирует приложение, например количество демо-сценариев). Если атрибута
   * нет или он пуст — считаем сами видимые элементы-источники. Пересчёт идёт
   * из refresh() и при изменениях содержимого; запись только при отличии
   * текста, чтобы не плодить мутации DOM.
   * ======================================================================== */

  function counterText(source) {
    var explicit = source.getAttribute('data-count-value');
    if (explicit !== null && String(explicit).replace(/\s+/g, '') !== '') {
      return String(explicit).replace(/^\s+|\s+$/g, '');
    }
    var count = 0;
    for (var i = 0; i < source.children.length; i++) {
      var child = source.children[i];
      if (child.hasAttribute('hidden')) { continue; }
      if (child.tagName === 'SCRIPT' || child.tagName === 'STYLE' || child.tagName === 'TEMPLATE') { continue; }
      count++;
    }
    return String(count);
  }

  function updateCounter(item) {
    var trigger = triggerOf(item);
    if (!trigger) { return; }
    var counter = trigger.querySelector(COUNT_SELECTOR);
    if (!counter) { return; }
    var content = contentOf(item);
    if (!content) { return; }
    var source = content.querySelector(SOURCE_SELECTOR);
    if (!source) { return; }
    var text = counterText(source);
    if (counter.textContent !== text) { counter.textContent = text; }
  }

  /* ==========================================================================
   * 6. Наблюдатель за DOM
   *
   * childList + subtree: .acc, добавленные приложением после загрузки,
   * инициализируются сами. Защита от циклов:
   *   - слушаются только добавления/удаления узлов (childList), наши правки
   *     атрибутов data-state и acc-anim мутаций не создают;
   *   - внутри .acc-content переинициализации нет — там живёт пользовательский
   *     контент; единственная реакция на такие мутации — пересчёт счётчика,
   *     а он пишет текст лишь при отличии и вне .acc-content, поэтому цикл
   *     невозможен;
   *   - refresh() идемпотентна: без реальных изменений новых мутаций не будет.
   * ======================================================================== */

  function ownerItemOf(node) {
    var el = node && node.nodeType === 1 ? node : (node ? node.parentNode : null);
    while (el && el.nodeType === 1) {
      if (el.tagName === 'DETAILS' && el.classList.contains('acc')) { return el; }
      el = el.parentNode;
    }
    return null;
  }

  function insideContent(node) {
    var el = node && node.nodeType === 1 ? node : (node ? node.parentNode : null);
    while (el && el.nodeType === 1) {
      if (el.classList && el.classList.contains('acc-content')) { return true; }
      el = el.parentNode;
    }
    return false;
  }

  function hasAccordion(node) {
    if (!node || node.nodeType !== 1) { return false; }
    if (node.tagName === 'DETAILS' && node.classList.contains('acc')) { return true; }
    return !!(node.querySelector && node.querySelector(ITEM_SELECTOR));
  }

  function onMutations(records) {
    var needRefresh = false;
    var counters = [];
    for (var i = 0; i < records.length; i++) {
      var record = records[i];
      if (insideContent(record.target)) {
        var owner = ownerItemOf(record.target);
        if (owner && counters.indexOf(owner) === -1) { counters.push(owner); }
        continue;
      }
      for (var j = 0; j < record.addedNodes.length; j++) {
        if (hasAccordion(record.addedNodes[j])) { needRefresh = true; }
      }
    }
    if (needRefresh) { refresh(); }
    for (var k = 0; k < counters.length; k++) { updateCounter(counters[k]); }
  }

  /* ==========================================================================
   * 7. Публичный API
   * ======================================================================== */

  /* Состояние пунктов с data-persist: { 'имя': 'open'|'closed' }.
     Дополнительно (необязательно) тот же ключ доступен в виде
     'acc:имя' — ровно так он лежит в localStorage, но в Object.keys
     и JSON попадает только короткая форма. */
  function getState() {
    var state = {};
    var items = document.querySelectorAll(ITEM_SELECTOR);
    for (var i = 0; i < items.length; i++) {
      var name = items[i].getAttribute('data-persist');
      if (name === null) { continue; }
      var value = items[i].open ? 'open' : 'closed';
      state[name] = value;
      if (Object.defineProperty) {
        try {
          Object.defineProperty(state, PERSIST_PREFIX + name, {
            value: value, enumerable: false, configurable: true, writable: true
          });
        } catch (e) {
          /* Алиас необязателен — игнорируем. */
        }
      }
    }
    return state;
  }

  /* Все .acc внутри контейнера (по умолчанию — документ). Вложенные
     аккордеоны обрабатываются как самостоятельные компоненты. */
  function itemsIn(container) {
    var root = (container === undefined || container === null) ? document : toElement(container);
    var list = [];
    if (!root) { return list; }
    if (root.nodeType === 1 && root.tagName === 'DETAILS' && root.classList.contains('acc')) {
      list.push(root);
    }
    if (!root.querySelectorAll) { return list; }
    var found = root.querySelectorAll(ITEM_SELECTOR);
    for (var i = 0; i < found.length; i++) {
      if (isNested(found[i], root)) { continue; }
      list.push(found[i]);
    }
    return list;
  }

  window.Accordion = {
    expand: function (target) {
      var item = itemFor(target);
      return item ? setOpen(ensureItem(item), true, true) : null;
    },
    collapse: function (target) {
      var item = itemFor(target);
      return item ? setOpen(ensureItem(item), false, true) : null;
    },
    toggle: function (target) {
      var item = itemFor(target);
      return item ? setOpen(ensureItem(item), !item.open, true) : null;
    },
    openAll: function (container) {
      var list = itemsIn(container);
      for (var i = 0; i < list.length; i++) { setOpen(ensureItem(list[i]), true, true); }
      return list;
    },
    closeAll: function (container) {
      var list = itemsIn(container);
      for (var i = 0; i < list.length; i++) { setOpen(ensureItem(list[i]), false, true); }
      return list;
    },
    refresh: refresh,
    getState: getState
  };

  /* ==========================================================================
   * 8. Запуск
   * ======================================================================== */

  function boot() {
    refresh();
    if (window.MutationObserver && document.body) {
      observer = new window.MutationObserver(onMutations);
      observer.observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
