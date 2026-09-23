/*
  Раздел «Финансы»: листы ввода, строки книги, графики. Без библиотек.

  Правила, которые здесь держатся (скиллы apple-design и emil-design-eng):
  * отклик — на нажатие, а не на отпускание;
  * лист уезжает тем же путём, что и приехал; на телефоне его можно
    смахнуть вниз пальцем — со скоростью, а не только по расстоянию;
  * необратимое — только удержанием (правило проекта);
  * после сохранения страница не перезагружается: новые строки въезжают
    на место, итоги пересчитывает сервер — числа никогда не расходятся;
  * «уменьшить движение» — анимации заменяются мгновенной сменой.
*/
(function () {
  'use strict';

  // Плавный переход между страницами «Финансов» (@view-transition в CSS).
  // Ушли на страницу без перехода или нажали дальше, не дождавшись, —
  // браузер отменяет переход и пишет в консоль «Transition was skipped».
  // Это не ошибка: гасим её, чтобы не путала при проверках.
  ['pageswap', 'pagereveal'].forEach(function (type) {
    window.addEventListener(type, function (e) {
      var vt = e.viewTransition;
      if (!vt) return;
      ['ready', 'finished', 'updateCallbackDone'].forEach(function (k) {
        if (vt[k] && vt[k].catch) vt[k].catch(function () {});
      });
    });
  });

  var root = document.getElementById('fin');
  if (!root) return;

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var NF = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var NF2 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var MAX_AMOUNT = 1000000;
  var MAX_FILE = 15 * 1024 * 1024;
  var MAX_ATTACH = 12;
  var MAX_ITEMS = 20;

  // Как на сервере (_rub): целые — без копеек, иначе ровно две цифры.
  function money(v) {
    var n = Math.round((Number(v) || 0) * 100) / 100;
    var a = Math.abs(n);
    var s = (a % 1 ? NF2 : NF).format(a) + '\u00a0₽';
    return n < 0 ? '−' + s : s;
  }
  function compact(v) {
    var n = Math.abs(v);
    var sign = v < 0 ? '−' : '';
    if (n >= 1e6) return sign + String(+(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)).replace('.', ',') + ' млн';
    if (n >= 1e3) return sign + Math.round(n / 1e3) + ' тыс';
    return sign + Math.round(n);
  }
  function plural(n, one, few, many) {
    n = Math.abs(n) % 100;
    var d = n % 10;
    if (n > 10 && n < 20) return many;
    if (d === 1) return one;
    if (d >= 2 && d <= 4) return few;
    return many;
  }
  function parseAmount(raw) {
    var text = String(raw || '').replace(/[\s  ]/g, '').replace(',', '.');
    if (!text || !/^\d*\.?\d*$/.test(text)) return null;
    var n = Math.round(parseFloat(text) * 100) / 100;
    return n > 0 && isFinite(n) ? n : null;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── всплывающее сообщение ────────────────────────────────────────────────
  var toastNode = null, toastTimer = null;
  function toast(text, isError) {
    if (toastNode) toastNode.remove();
    clearTimeout(toastTimer);
    var node = document.createElement('div');
    node.className = 'fin-toast' + (isError ? ' is-error' : '');
    node.setAttribute('role', 'status');
    node.innerHTML = '<i class="ph ph-' + (isError ? 'warning-circle' : 'check-circle') + '"></i><span></span>';
    node.lastChild.textContent = text;
    document.body.appendChild(node);
    toastNode = node;
    toastTimer = setTimeout(function () {
      node.classList.add('is-leaving');
      setTimeout(function () { node.remove(); if (toastNode === node) toastNode = null; }, 260);
    }, isError ? 4200 : 2600);
  }

  // ── обновить страницу без перезагрузки ───────────────────────────────────
  // Итоги, дни и графики считает сервер. После любого изменения берём свежую
  // страницу и подменяем живые области — так числа в итогах и в строках
  // никогда не разойдутся с базой.
  function refreshLive(extra) {
    var url = new URL(location.href);
    url.searchParams.delete('new');
    Object.keys(extra || {}).forEach(function (k) {
      if (extra[k]) url.searchParams.set(k, extra[k]); else url.searchParams.delete(k);
    });
    return fetch(url.toString(), { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        document.querySelectorAll('[data-fin-live]').forEach(function (node) {
          var fresh = doc.querySelector('[data-fin-live="' + node.getAttribute('data-fin-live') + '"]');
          if (fresh) node.replaceWith(document.importNode(fresh, true));
        });
        // «Обзор» при следующем обновлении не должен заново въезжать целиком.
        document.querySelectorAll('.fin-rise').forEach(function (n) { n.classList.remove('fin-rise'); });
        renderCharts(false);
      });
  }

  // ── строки книги ─────────────────────────────────────────────────────────
  function toggleRow(row) {
    var open = !row.classList.contains('is-open');
    row.classList.toggle('is-open', open);
    row.querySelector('.fin-row__main').setAttribute('aria-expanded', String(open));
    var more = row.querySelector('.fin-row__more');
    if (open) more.removeAttribute('inert'); else more.setAttribute('inert', '');
  }

  function collapseRow(row) {
    return new Promise(function (done) {
      var day = row.closest('.fin-day');
      var finished = false;
      function end() {
        if (finished) return;
        finished = true;
        row.remove();
        if (day && !day.querySelector('.fin-row')) day.remove();
        done();
      }
      if (reduce.matches) { row.style.opacity = '0'; setTimeout(end, 160); return; }
      row.style.height = row.getBoundingClientRect().height + 'px';
      void row.offsetHeight;                       // зафиксировать высоту до перехода
      row.classList.add('is-leaving');
      row.style.height = '0px';
      row.addEventListener('transitionend', function (e) { if (e.propertyName === 'height') end(); });
      setTimeout(end, 420);
    });
  }

  function setBusy(btn, busy) {
    btn.classList.toggle('is-busy', busy);
    btn.disabled = busy;
    var spin = btn.querySelector('.fin-spin');
    if (busy && !spin) {
      spin = document.createElement('span');
      spin.className = 'fin-spin';
      btn.insertBefore(spin, btn.firstChild);
    } else if (!busy && spin) {
      spin.remove();
    }
  }

  // Решение по трате водителя — тот же адрес, что у приложения и бота:
  // одно решение, одни последствия, где бы владелец ни нажал.
  function decide(btn) {
    var row = btn.closest('.fin-row');
    var action = btn.getAttribute('data-decide');
    row.querySelectorAll('[data-decide]').forEach(function (b) { b.disabled = true; });
    setBusy(btn, true);
    var body = new FormData();
    body.append('action', action);
    fetch('/api/expenses/' + row.dataset.id + '/decision', { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (res) { if (!res.ok) throw new Error(res.status); return res.json(); })
      .then(function (data) {
        toast(data.already_decided ? 'Решение уже было принято: ' + data.label.toLowerCase()
                                   : (data.status === 'approved' ? 'Одобрено' : 'Отклонено'));
        return refreshLive({ added: row.dataset.id });
      })
      .catch(function () {
        setBusy(btn, false);
        row.querySelectorAll('[data-decide]').forEach(function (b) { b.disabled = false; });
        toast('Не получилось. Проверьте связь и попробуйте ещё раз.', true);
      });
  }

  // ── удаление удержанием ──────────────────────────────────────────────────
  var HOLD_MS = 1000;
  function startHold(btn) {
    if (btn.dataset.state === 'busy' || btn._hold) return;
    btn.classList.add('is-holding');
    btn._hold = setTimeout(function () {
      btn._hold = null;
      btn.classList.remove('is-holding');
      doDelete(btn);
    }, reduce.matches ? 600 : HOLD_MS);
  }
  function cancelHold(btn) {
    if (!btn || !btn._hold) return;
    clearTimeout(btn._hold);
    btn._hold = null;
    btn.classList.remove('is-holding');
  }
  function doDelete(btn) {
    var row = btn.closest('.fin-row');
    btn.dataset.state = 'busy';
    if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) { /* нет — и ладно */ } }
    fetch(btn.getAttribute('data-delete-url'), { method: 'POST', credentials: 'same-origin' })
      .then(function (res) { if (!res.ok) throw new Error(res.status); })
      .then(function () { return collapseRow(row); })
      .then(function () {
        toast('Удалено');
        return refreshLive();
      })
      .catch(function () {
        btn.dataset.state = '';
        toast('Не удалось удалить. Попробуйте ещё раз.', true);
      });
  }

  var holding = null;
  document.addEventListener('pointerdown', function (e) {
    var btn = e.target.closest('.fin-hold');
    if (!btn || e.button !== 0) return;
    holding = btn;
    startHold(btn);
  });
  ['pointerup', 'pointercancel'].forEach(function (type) {
    document.addEventListener(type, function () { cancelHold(holding); holding = null; });
  });
  document.addEventListener('pointerout', function (e) {
    // Палец/мышь ушли с кнопки — отмена, как у iOS: передумал — увёл палец.
    if (holding && e.target.closest('.fin-hold') === holding && !holding.contains(e.relatedTarget)) {
      cancelHold(holding);
      holding = null;
    }
  });
  document.addEventListener('contextmenu', function (e) { if (e.target.closest('.fin-hold')) e.preventDefault(); });
  document.addEventListener('keydown', function (e) {
    var btn = e.target.closest && e.target.closest('.fin-hold');
    if (btn && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); if (!e.repeat) startHold(btn); }
  });
  document.addEventListener('keyup', function (e) {
    var btn = e.target.closest && e.target.closest('.fin-hold');
    if (btn && (e.key === ' ' || e.key === 'Enter')) cancelHold(btn);
  });

  // ── общие нажатия ────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var t = e.target;
    var main = t.closest('.fin-row__main');
    if (main) { toggleRow(main.closest('.fin-row')); return; }
    var dec = t.closest('[data-decide]');
    if (dec) { decide(dec); return; }
    var opener = t.closest('[data-open-sheet]');
    if (opener) { openSheet(opener.getAttribute('data-open-sheet')); return; }
    var seg = t.closest('.fin-seg a');
    if (seg && !e.metaKey && !e.ctrlKey) {
      // Отклик сразу: вкладка становится выбранной до того, как придёт страница.
      seg.parentNode.querySelectorAll('a').forEach(function (a) { a.removeAttribute('aria-current'); });
      seg.setAttribute('aria-current', 'page');
    }
  });

  // Отборы применяются сразу при выборе — без кнопки «Показать».
  document.addEventListener('change', function (e) {
    var form = e.target.form;
    if (form && form.hasAttribute('data-autosubmit')) {
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.name && !el.value) el.disabled = true;     // пустые отборы не тащим в адрес
      });
      form.submit();
    }
  });

  // Свои даты: закрыть всплывашку по щелчку мимо.
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.fin-custom[open]').forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });

  // ── лист ввода ───────────────────────────────────────────────────────────
  function isBottomSheet() { return window.matchMedia('(max-width: 700px)').matches; }

  function openSheet(name) {
    var dlg = document.getElementById('sheet-' + name);
    if (!dlg || dlg.open || typeof dlg.showModal !== 'function') return;
    dlg.classList.remove('is-closing');
    dlg.style.transform = '';
    dlg.showModal();
    if (name === 'expense') expense.onOpen();
    if (name === 'income') income.onOpen();
  }

  function closeSheet(dlg) {
    if (!dlg.open || dlg.classList.contains('is-closing')) return;
    dlg.classList.add('is-closing');
    var done = false;
    function end() {
      if (done) return;
      done = true;
      dlg.close();
      dlg.classList.remove('is-closing');
      dlg.style.transform = '';
      dlg.style.transition = '';
    }
    dlg.addEventListener('transitionend', function handler(e) {
      if (e.target === dlg && (e.propertyName === 'transform' || e.propertyName === 'opacity')) {
        dlg.removeEventListener('transitionend', handler);
        end();
      }
    });
    setTimeout(end, 340);
  }

  document.querySelectorAll('dialog.fin-sheet').forEach(function (dlg) {
    dlg.addEventListener('cancel', function (e) { e.preventDefault(); closeSheet(dlg); });
    dlg.addEventListener('click', function (e) {
      if (e.target === dlg) closeSheet(dlg);                 // щелчок по затемнению
      if (e.target.closest('[data-close]')) closeSheet(dlg);
    });
    enableDrag(dlg);
  });

  // Смахнуть лист вниз (телефон). Лист идёт за пальцем 1:1, вверх — с
  // сопротивлением; отпустили быстро или дальше трети — закрываем.
  function enableDrag(dlg) {
    var handle = dlg.querySelector('.fin-sheet__head');
    var grab = dlg.querySelector('.fin-sheet__grab');
    var startY = 0, lastY = 0, lastT = 0, speed = 0, dragging = false;
    function rubber(x) { var d = 120; return (x * d * 0.55) / (d + 0.55 * x); }
    function down(e) {
      if (!isBottomSheet() || e.target.closest('button')) return;
      dragging = true;
      startY = lastY = e.clientY;
      lastT = e.timeStamp;
      speed = 0;
      e.currentTarget.setPointerCapture(e.pointerId);
      dlg.style.transition = 'none';
    }
    function move(e) {
      if (!dragging) return;
      var dy = e.clientY - startY;
      var y = dy >= 0 ? dy : -rubber(-dy);
      dlg.style.transform = 'translateY(' + y + 'px)';
      var dt = e.timeStamp - lastT;
      if (dt > 0) speed = (e.clientY - lastY) / dt;
      lastY = e.clientY;
      lastT = e.timeStamp;
    }
    function up() {
      if (!dragging) return;
      dragging = false;
      var dy = lastY - startY;
      var h = dlg.getBoundingClientRect().height || 1;
      dlg.style.transition = '';
      if (dy > h * 0.3 || speed > 0.5) {
        dlg.style.transform = 'translateY(100%)';
        closeSheet(dlg);
      } else {
        dlg.style.transform = '';
      }
    }
    [handle, grab].forEach(function (node) {
      if (!node) return;
      node.addEventListener('pointerdown', down);
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerup', up);
      node.addEventListener('pointercancel', up);
    });
  }

  function formError(form, text) {
    var box = form.querySelector('[data-error]');
    if (box) box.textContent = text || '';
  }

  // ── лист «Новый расход» ──────────────────────────────────────────────────
  var expense = (function () {
    var form = document.getElementById('expense-form');
    if (!form) return { onOpen: function () {} };
    var dlg = form.closest('dialog');
    var box = form.querySelector('#fin-items');
    var tpl = dlg.querySelector('#fin-item-tpl');
    var addBtn = form.querySelector('[data-add-item]');
    var submit = form.querySelector('[data-submit]');
    var seq = 0;

    function items() { return Array.prototype.slice.call(box.querySelectorAll('.fin-item:not(.is-leaving)')); }

    function renumber() {
      var list = items();
      box.dataset.count = String(list.length);
      list.forEach(function (node, i) { node.querySelector('[data-n]').textContent = i + 1; });
      addBtn.hidden = list.length >= MAX_ITEMS;
      updateTotal();
    }

    function addItem(focus) {
      var node = tpl.content.firstElementChild.cloneNode(true);
      var uid = ++seq;
      node._files = [];
      node.querySelectorAll('.fin-cat input').forEach(function (input) { input.name = 'cat_' + uid; });
      box.appendChild(node);
      renumber();
      if (focus) {
        node.scrollIntoView({ behavior: reduce.matches ? 'auto' : 'smooth', block: 'nearest' });
      }
      return node;
    }

    function removeItem(node) {
      if (items().length <= 1) return;
      node.classList.add('is-leaving');
      renumber();
      node._files.forEach(function (f) { if (f.url) URL.revokeObjectURL(f.url); });
      setTimeout(function () { node.remove(); }, reduce.matches ? 0 : 230);
    }

    function updateTotal() {
      var list = items();
      var sum = 0, filled = 0;
      list.forEach(function (node) {
        var v = parseAmount(node.querySelector('[data-amount]').value);
        if (v) { sum += v; filled += 1; }
      });
      form.querySelector('[data-total]').textContent = money(sum);
      // ⚠️ Не [data-count]: так помечен и сам список трат (#fin-items), и
      // подпись затёрла бы все траты разом.
      form.querySelector('[data-total-label]').textContent = list.length > 1
        ? 'Итого · ' + list.length + ' ' + plural(list.length, 'трата', 'траты', 'трат')
        : 'Итого';
    }

    function itemError(node, text) {
      node.querySelector('.fin-item__err').textContent = text || '';
      node.classList.toggle('is-invalid', !!text);
    }

    function shake(node) {
      if (reduce.matches) return;
      node.classList.remove('fin-shake');
      void node.offsetWidth;
      node.classList.add('fin-shake');
    }

    // Фото с телефона — 5–10 МБ. Уменьшаем до 2000 px по длинной стороне:
    // чек читается, а база не пухнет. Не вышло (HEIC, старый браузер) —
    // отправляем как есть.
    function shrink(file) {
      if (!/^image\/(jpeg|png|webp)$/i.test(file.type) || !window.createImageBitmap) return Promise.resolve(null);
      return createImageBitmap(file).then(function (bmp) {
        var side = Math.max(bmp.width, bmp.height);
        var k = Math.min(1, 2000 / side);
        if (k === 1 && file.size < 1.5 * 1024 * 1024) { bmp.close && bmp.close(); return null; }
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(bmp.width * k);
        canvas.height = Math.round(bmp.height * k);
        canvas.getContext('2d').drawImage(bmp, 0, 0, canvas.width, canvas.height);
        bmp.close && bmp.close();
        return new Promise(function (resolve) {
          canvas.toBlob(function (blob) { resolve(blob && blob.size < file.size ? blob : null); }, 'image/jpeg', 0.85);
        });
      }).catch(function () { return null; });
    }

    function addFile(node, kind, file) {
      if (node._files.length >= MAX_ATTACH) { itemError(node, 'Не больше ' + MAX_ATTACH + ' вложений на одну трату.'); return; }
      var entry = { kind: kind, blob: file, name: file.name || (kind === 'photo' ? 'photo.jpg' : 'file'), url: null, ready: Promise.resolve() };
      var li = document.createElement('li');
      li.className = 'fin-thumb' + (kind === 'photo' ? '' : ' fin-thumb--file');
      if (kind === 'photo') {
        entry.url = URL.createObjectURL(file);
        li.innerHTML = '<img alt="">';
        li.firstChild.src = entry.url;
        li.firstChild.alt = entry.name;
      } else {
        li.innerHTML = '<i class="ph ph-' + (/\.pdf$/i.test(entry.name) ? 'file-pdf' : 'file') + '"></i><span></span>';
        li.querySelector('span').textContent = entry.name;
      }
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'fin-thumb__x';
      x.setAttribute('aria-label', 'Убрать ' + entry.name);
      x.innerHTML = '<i class="ph ph-x"></i>';
      li.appendChild(x);
      li._entry = entry;
      node._files.push(entry);
      node.querySelector('.fin-thumbs').appendChild(li);
      itemError(node, '');

      if (kind === 'photo') {
        li.classList.add('is-busy');
        entry.ready = shrink(file).then(function (small) {
          if (small) {
            entry.blob = small;
            entry.name = entry.name.replace(/\.[^.]+$/, '') + '.jpg';
          }
        }).finally(function () { li.classList.remove('is-busy'); });
      }
      entry.ready = entry.ready.then(function () {
        if (entry.blob.size > MAX_FILE) {
          removeFile(node, li);
          itemError(node, '«' + entry.name + '» больше 15 МБ — такой файл не примем.');
        }
      });
    }

    function removeFile(node, li) {
      var entry = li._entry;
      node._files = node._files.filter(function (f) { return f !== entry; });
      if (entry.url) URL.revokeObjectURL(entry.url);
      li.classList.add('is-leaving');
      setTimeout(function () { li.remove(); }, reduce.matches ? 0 : 200);
    }

    function addCategoryChip(code, label) {
      var html = '<label class="fin-cat is-fresh"><input type="radio" value="' + esc(code) + '">' +
                 '<span><i class="ph ph-tag" aria-hidden="true"></i>' + esc(label) + '</span></label>';
      // В шаблон — чтобы и следующие траты знали новый вид.
      var tplAdd = tpl.content.querySelector('[data-new-cat]');
      if (!tpl.content.querySelector('.fin-cat input[value="' + CSS.escape(code) + '"]')) {
        tplAdd.insertAdjacentHTML('beforebegin', html.replace(' is-fresh', ''));
      }
      items().forEach(function (node, i) {
        if (node.querySelector('.fin-cat input[value="' + CSS.escape(code) + '"]')) return;
        node.querySelector('[data-new-cat]').insertAdjacentHTML('beforebegin', html);
        var input = node.querySelector('.fin-cat input[value="' + CSS.escape(code) + '"]');
        input.name = node.querySelector('.fin-cat input').name;
      });
    }

    function saveCategory(node) {
      var wrap = node.querySelector('.fin-newcat');
      var input = wrap.querySelector('input');
      var name = input.value.trim();
      if (!name) { input.focus(); return; }
      var btn = wrap.querySelector('[data-save-cat]');
      setBusy(btn, true);
      var body = new FormData();
      body.append('name', name);
      fetch('/finances/categories', { method: 'POST', body: body, credentials: 'same-origin' })
        .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
        .then(function (r) {
          setBusy(btn, false);
          if (!r.ok || !r.data.ok) { itemError(node, (r.data && r.data.message) || 'Не получилось добавить вид.'); return; }
          addCategoryChip(r.data.code, r.data.label);
          var chosen = node.querySelector('.fin-cat input[value="' + CSS.escape(r.data.code) + '"]');
          if (chosen) chosen.checked = true;
          input.value = '';
          wrap.hidden = true;
          itemError(node, '');
        })
        .catch(function () { setBusy(btn, false); itemError(node, 'Нет связи. Попробуйте ещё раз.'); });
    }

    function localNow() {
      var d = new Date();
      d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      return d.toISOString().slice(0, 16);
    }

    function reset() {
      items().forEach(function (node) {
        node._files.forEach(function (f) { if (f.url) URL.revokeObjectURL(f.url); });
        node.remove();
      });
      form.querySelectorAll('input[name="payment_method"]').forEach(function (i) { i.checked = false; });
      form.querySelector('[name="supplier"]').value = '';
      form.querySelector('[name="spent_at"]').value = localNow();
      formError(form, '');
      addItem(false);
    }

    // Клик по уже выбранному способу оплаты снимает выбор: поле необязательное.
    form.addEventListener('pointerdown', function (e) {
      var label = e.target.closest('.fin-cat--pay');
      if (label) label.querySelector('input').dataset.was = label.querySelector('input').checked ? '1' : '';
    });
    form.addEventListener('click', function (e) {
      var t = e.target;
      if (t.matches('.fin-cat--pay input') && t.dataset.was === '1') { t.checked = false; t.dataset.was = ''; }
      if (t.closest('[data-add-item]')) { addItem(true); return; }
      var rm = t.closest('[data-remove-item]');
      if (rm) { removeItem(rm.closest('.fin-item')); return; }
      var nc = t.closest('[data-new-cat]');
      if (nc) {
        var wrap = nc.closest('.fin-item').querySelector('.fin-newcat');
        wrap.hidden = !wrap.hidden;
        if (!wrap.hidden) wrap.querySelector('input').focus();
        return;
      }
      var sc = t.closest('[data-save-cat]');
      if (sc) { saveCategory(sc.closest('.fin-item')); return; }
      var x = t.closest('.fin-thumb__x');
      if (x) { removeFile(x.closest('.fin-item'), x.closest('.fin-thumb')); return; }
      if (t.closest('[data-now]')) { form.querySelector('[name="spent_at"]').value = localNow(); }
    });
    form.addEventListener('keydown', function (e) {
      // Enter в поле своего вида — добавить вид, а не отправить весь лист.
      if (e.key === 'Enter' && e.target.closest('.fin-newcat')) {
        e.preventDefault();
        saveCategory(e.target.closest('.fin-item'));
      }
    });
    form.addEventListener('input', function (e) {
      if (e.target.matches('[data-amount]')) updateTotal();
      var item = e.target.closest('.fin-item');
      if (item && item.classList.contains('is-invalid')) itemError(item, '');
      formError(form, '');
    });
    form.addEventListener('change', function (e) {
      var t = e.target;
      if (t.name === 'link') {
        var reveal = form.querySelector('[data-reveal="vehicle"]');
        reveal.hidden = t.value !== 'vehicle';
      }
      if (t.matches('[data-att]')) {
        var node = t.closest('.fin-item');
        Array.prototype.forEach.call(t.files || [], function (file) { addFile(node, t.getAttribute('data-att'), file); });
        t.value = '';
      }
      if (t.matches('.fin-cat input')) {
        var it = t.closest('.fin-item');
        if (it) itemError(it, '');
      }
    });

    function collect() {
      var result = [], firstBad = null;
      items().forEach(function (node) {
        var checked = node.querySelector('.fin-cat input:checked');
        var amount = parseAmount(node.querySelector('[data-amount]').value);
        var comment = node.querySelector('[data-comment]').value.trim();
        var problem = '';
        if (!checked) problem = 'Выберите вид расхода.';
        else if (!amount) problem = 'Укажите сумму больше нуля.';
        else if (amount > MAX_AMOUNT) problem = 'Не больше 1 000 000 ₽ за раз.';
        else if (checked.value === 'other' && !comment) problem = 'Для «Прочего» коротко напишите, что это.';
        itemError(node, problem);
        if (problem && !firstBad) firstBad = node;
        result.push({ node: node, category: checked ? checked.value : '', amount: amount, description: comment });
      });
      return { items: result, firstBad: firstBad };
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submit.disabled) return;
      var c = collect();
      if (c.firstBad) {
        shake(c.firstBad);
        c.firstBad.scrollIntoView({ behavior: reduce.matches ? 'auto' : 'smooth', block: 'center' });
        return;
      }
      setBusy(submit, true);
      formError(form, '');
      var total = c.items.reduce(function (s, it) { return s + it.amount; }, 0);
      var pending = [];
      c.items.forEach(function (it) { it.node._files.forEach(function (f) { pending.push(f.ready); }); });
      Promise.all(pending).then(function () {
        var body = new FormData();
        body.append('items', JSON.stringify(c.items.map(function (it) {
          return { category: it.category, amount: String(it.amount), description: it.description };
        })));
        ['spent_at', 'supplier', 'vehicle_id', 'trip_id', 'shift_id'].forEach(function (name) {
          var el = form.querySelector('[name="' + name + '"]');
          if (el && el.value) body.append(name, el.value);
        });
        var link = form.querySelector('[name="link"]:checked');
        body.append('link', link ? link.value : 'company');
        var pay = form.querySelector('[name="payment_method"]:checked');
        if (pay) body.append('payment_method', pay.value);
        c.items.forEach(function (it, i) {
          it.node._files.forEach(function (f) {
            body.append((f.kind === 'photo' ? 'photos_' : 'files_') + i, f.blob, f.name);
          });
        });
        return fetch('/finances/expenses', { method: 'POST', body: body, credentials: 'same-origin' });
      }).then(function (res) {
        return res.json().catch(function () { return { ok: false, message: 'Сервер ответил странно (' + res.status + ').' }; });
      }).then(function (data) {
        if (!data.ok) {
          setBusy(submit, false);
          formError(form, data.message || 'Не получилось сохранить.');
          return;
        }
        closeSheet(dlg);
        setBusy(submit, false);
        var n = data.ids.length;
        setTimeout(reset, 360);
        return refreshLive({ added: data.ids.join(',') }).then(function () {
          toast(n > 1 ? 'Внесено ' + n + ' ' + plural(n, 'трата', 'траты', 'трат') + ' на ' + money(total)
                      : 'Расход ' + money(total) + ' внесён');
        });
      }).catch(function () {
        setBusy(submit, false);
        formError(form, 'Нет связи с сервером. Данные остались в форме — попробуйте ещё раз.');
      });
    });

    return {
      onOpen: function () {
        if (!items().length) addItem(false);
        // Адрес с ?new=1 открывает лист. Убираем метку, чтобы обновление
        // страницы не открывало его снова.
        var url = new URL(location.href);
        if (url.searchParams.has('new')) {
          url.searchParams.delete('new');
          history.replaceState(null, '', url.toString());
        }
      }
    };
  })();

  // ── лист «Новое поступление» ─────────────────────────────────────────────
  var income = (function () {
    var form = document.getElementById('income-form');
    if (!form) return { onOpen: function () {} };
    var dlg = form.closest('dialog');
    var submit = form.querySelector('[data-submit]');
    var amount = form.querySelector('[data-amount]');

    form.addEventListener('pointerdown', function (e) {
      var label = e.target.closest('.fin-cat--pay');
      if (label) label.querySelector('input').dataset.was = label.querySelector('input').checked ? '1' : '';
    });
    form.addEventListener('click', function (e) {
      var t = e.target;
      if (t.matches('.fin-cat--pay input') && t.dataset.was === '1') { t.checked = false; t.dataset.was = ''; }
    });
    form.addEventListener('input', function () {
      form.querySelector('[data-total]').textContent = money(parseAmount(amount.value) || 0);
      formError(form, '');
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submit.disabled) return;
      var value = parseAmount(amount.value);
      if (!value) {
        formError(form, 'Укажите сумму больше нуля.');
        var box = amount.closest('.fin-amount');
        if (!reduce.matches) { box.classList.remove('fin-shake'); void box.offsetWidth; box.classList.add('fin-shake'); }
        amount.focus();
        return;
      }
      setBusy(submit, true);
      var body = new FormData(form);
      body.set('amount', String(value));
      fetch('/finances/income', { method: 'POST', body: body, credentials: 'same-origin' })
        .then(function (res) { return res.json().catch(function () { return { ok: false }; }); })
        .then(function (data) {
          setBusy(submit, false);
          if (!data.ok) { formError(form, data.message || 'Не получилось сохранить.'); return; }
          closeSheet(dlg);
          setTimeout(function () {
            amount.value = '';
            form.querySelector('[name="description"]').value = '';
            form.querySelectorAll('[name="category"]').forEach(function (i) { i.checked = false; });
            form.querySelector('[data-total]').textContent = money(0);
          }, 360);
          return refreshLive({ added: data.id }).then(function () { toast('Поступление ' + money(value) + ' внесено'); });
        })
        .catch(function () {
          setBusy(submit, false);
          formError(form, 'Нет связи с сервером. Попробуйте ещё раз.');
        });
    });
    return { onOpen: function () {} };
  })();

  // ── графики ──────────────────────────────────────────────────────────────
  var chartData = null;
  function readData() {
    var node = document.getElementById('fin-data');
    if (!node) return null;
    try { return JSON.parse(node.textContent); } catch (e) { return null; }
  }

  function niceTop(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var f = v / p;
    var n = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return n * p;
  }

  // Денежный поток: пара столбиков на день/неделю/месяц — доход и расход,
  // расход сложен из топлива (снизу, синий) и прочего (сверху, оранжевый).
  function drawCashflow(box, cf, animate) {
    var mode = box.getAttribute('data-mode') || 'both';
    var W = Math.round(box.clientWidth);
    if (!cf || !cf.labels || !cf.labels.length || W < 60) return;
    var inc = cf.income || [], exp = cf.expense || [], fuel = cf.fuel || [];
    var two = mode !== 'income';
    var peak = Math.max.apply(null, inc.concat(two ? exp : []).concat([0]));
    if (peak <= 0) {
      box.innerHTML = '<p class="fin-chart__empty">За этот период денег не было</p>';
      return;
    }
    var H = 240, padL = 54, padR = 6, padT = 14, padB = 26;
    var pw = W - padL - padR, ph = H - padT - padB;
    var n = cf.labels.length, gw = pw / n;
    var bw = Math.max(2, Math.min(two ? 16 : 26, gw * (two ? 0.34 : 0.56)));
    var top = niceTop(peak);
    var y = function (v) { return padT + ph - (v / top) * ph; };
    var out = ['<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" aria-hidden="true">'];
    [0, 0.5, 1].forEach(function (k) {
      var yy = Math.round(y(top * k)) + 0.5;
      out.push('<line class="fin-axis' + (k === 0 ? ' fin-axis--zero' : '') + '" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yy + '" y2="' + yy + '"/>');
      out.push('<text class="fin-tick" x="' + (padL - 8) + '" y="' + (yy + 4) + '" text-anchor="end">' + (k === 0 ? '0' : compact(top * k)) + '</text>');
    });
    var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 58))));
    function rect(x, v0, v1, color, i) {
      var h = Math.max(0, y(v0) - y(v1));
      if (h <= 0) return '';
      var r = Math.min(3, bw / 2, h / 2);
      return '<rect class="fin-bar-r" style="--i:' + i + '" x="' + x.toFixed(1) + '" y="' + y(v1).toFixed(1) +
             '" width="' + bw.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="' + r.toFixed(1) + '" fill="' + color + '"/>';
    }
    for (var i = 0; i < n; i++) {
      var gx = padL + i * gw, cx = gx + gw / 2;
      out.push('<g class="fin-col" data-i="' + i + '">');
      out.push('<rect class="fin-band" x="' + gx.toFixed(1) + '" y="' + padT + '" width="' + gw.toFixed(1) + '" height="' + ph + '" rx="6"/>');
      if (two) {
        out.push(rect(cx - bw - 1.5, 0, inc[i] || 0, '#34c759', i));
        var f = Math.min(fuel[i] || 0, exp[i] || 0);
        out.push(rect(cx + 1.5, 0, f, '#0071e3', i));
        out.push(rect(cx + 1.5, f, exp[i] || 0, '#ff9f0a', i));
      } else {
        out.push(rect(cx - bw / 2, 0, inc[i] || 0, '#34c759', i));
      }
      out.push('</g>');
      if (i % every === 0) {
        out.push('<text class="fin-tick" x="' + cx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + esc(cf.labels[i]) + '</text>');
      }
    }
    out.push('</svg><div class="fin-tip" aria-hidden="true"></div>');
    box.innerHTML = out.join('');
    if (animate && !reduce.matches) {
      box.classList.add('is-drawing');
      setTimeout(function () { box.classList.remove('is-drawing'); }, 560 + n * 12 + 50);
    }

    var tip = box.querySelector('.fin-tip');
    var svg = box.querySelector('svg');
    var current = -1;
    function show(clientX) {
      var rectBox = svg.getBoundingClientRect();
      var x = (clientX - rectBox.left) * (W / rectBox.width);
      var i = Math.floor((x - padL) / gw);
      if (i < 0 || i >= n) { hide(); return; }
      if (i !== current) {
        var prev = svg.querySelector('.fin-col.is-hover');
        if (prev) prev.classList.remove('is-hover');
        svg.querySelector('.fin-col[data-i="' + i + '"]').classList.add('is-hover');
        current = i;
        var lines = '<b>' + esc(cf.labels[i]) + '</b><span><i>Доход</i>' + money(inc[i] || 0) + '</span>';
        if (two) {
          lines += '<span><i>Расход</i>' + money(exp[i] || 0) + '</span>';
          if (fuel[i]) lines += '<span><i>из них топливо</i>' + money(fuel[i]) + '</span>';
          lines += '<span><i>Итог</i>' + money((inc[i] || 0) - (exp[i] || 0)) + '</span>';
        }
        tip.innerHTML = lines;
        var cx = padL + i * gw + gw / 2;
        var peakHere = Math.max(inc[i] || 0, two ? (exp[i] || 0) : 0);
        var left = Math.max(80, Math.min(W - 80, cx));
        tip.style.left = (left * rectBox.width / W) + 'px';
        tip.style.top = (Math.max(y(peakHere), padT + 30) * rectBox.height / H) + 'px';
      }
      tip.classList.add('is-on');
    }
    function hide() {
      tip.classList.remove('is-on');
      var prev = svg.querySelector('.fin-col.is-hover');
      if (prev) prev.classList.remove('is-hover');
      current = -1;
    }
    svg.addEventListener('pointermove', function (e) { show(e.clientX); });
    svg.addEventListener('pointerdown', function (e) { show(e.clientX); });
    svg.addEventListener('pointerleave', hide);
  }

  // Кольцо «Куда уходят деньги»: наведение на кусок или строку легенды
  // показывает вид и долю в центре; нажатие ведёт в расходы этого вида.
  function drawDonut(box, cats, animate) {
    if (!cats || !cats.length) {
      box.innerHTML = '<p class="fin-chart__empty">Одобренных расходов за период нет</p>';
      return;
    }
    var total = cats.reduce(function (s, c) { return s + c.amount; }, 0);
    var R = 72, SW = 22, C = 2 * Math.PI * R;
    var gap = cats.length > 1 ? 2 : 0;
    var link = box.getAttribute('data-link') || '';
    var off = 0;
    var segs = cats.map(function (c, i) {
      var len = Math.max(0, (c.amount / total) * C - gap);
      var s = '<circle class="fin-donut__seg" data-i="' + i + '" cx="92" cy="92" r="' + R + '" stroke="' + esc(c.color) +
              '" stroke-width="' + SW + '" stroke-dasharray="' + len.toFixed(2) + ' ' + C.toFixed(2) +
              '" stroke-dashoffset="' + (-off).toFixed(2) + '"/>';
      off += (c.amount / total) * C;
      return s;
    }).join('');
    var legend = cats.map(function (c, i) {
      return '<li><a href="' + esc(link + encodeURIComponent(c.code)) + '" data-i="' + i + '">' +
             '<i class="fin-dot" style="--c:' + esc(c.color) + '"></i><span>' + esc(c.label) + '</span>' +
             '<b>' + money(c.amount) + '</b><small>' + Math.round(c.share) + '&nbsp;%</small></a></li>';
    }).join('');
    box.innerHTML =
      '<div class="fin-donut__ring"><svg viewBox="0 0 184 184" aria-hidden="true">' + segs + '</svg>' +
      '<div class="fin-donut__center"><b>' + compact(total) + '&nbsp;₽</b><span>все расходы</span></div></div>' +
      '<ul class="fin-donut__legend">' + legend + '</ul>';
    if (animate && !reduce.matches) {
      box.classList.add('is-drawing');
      setTimeout(function () { box.classList.remove('is-drawing'); }, 820);
    }
    var center = box.querySelector('.fin-donut__center');
    function focus(i) {
      box.classList.toggle('has-focus', i >= 0);
      box.querySelectorAll('.fin-donut__seg').forEach(function (s) { s.classList.toggle('is-on', +s.dataset.i === i); });
      box.querySelectorAll('.fin-donut__legend a').forEach(function (a) { a.classList.toggle('is-on', +a.dataset.i === i); });
      if (i >= 0) {
        center.innerHTML = '<b>' + Math.round(cats[i].share) + '&nbsp;%</b><span>' + esc(cats[i].label) + '<br>' + money(cats[i].amount) + '</span>';
      } else {
        center.innerHTML = '<b>' + compact(total) + '&nbsp;₽</b><span>все расходы</span>';
      }
    }
    box.addEventListener('pointerover', function (e) {
      var hit = e.target.closest('[data-i]');
      focus(hit ? +hit.dataset.i : -1);
    });
    box.addEventListener('pointerleave', function () { focus(-1); });
    box.addEventListener('focusin', function (e) { var hit = e.target.closest('[data-i]'); if (hit) focus(+hit.dataset.i); });
    box.addEventListener('focusout', function () { focus(-1); });
    box.querySelector('svg').addEventListener('click', function (e) {
      var seg = e.target.closest('.fin-donut__seg');
      if (seg) location.href = link + encodeURIComponent(cats[+seg.dataset.i].code);
    });
  }

  // Топливо нарастающим итогом: видно темп трат, а не зубцы «заправка —
  // не заправка» по дням.
  function drawSpark(box, daily) {
    var W = Math.round(box.clientWidth), H = 64;
    if (!daily || daily.length < 2 || W < 40) { box.innerHTML = ''; return; }
    var run = 0;
    var series = daily.map(function (v) { run += v || 0; return run; });
    var peak = Math.max.apply(null, series);
    if (peak <= 0) { box.innerHTML = ''; return; }
    var step = W / (series.length - 1);
    var pts = series.map(function (v, i) { return [i * step, 4 + (H - 8) * (1 - v / peak)]; });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
    var area = line + ' L' + W + ' ' + H + ' L0 ' + H + ' Z';
    box.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '">' +
      '<defs><linearGradient id="fin-spark-fill" x1="0" x2="0" y1="0" y2="1">' +
      '<stop offset="0" stop-color="#0071e3" stop-opacity=".18"/><stop offset="1" stop-color="#0071e3" stop-opacity="0"/>' +
      '</linearGradient></defs><path class="fin-spark__area" d="' + area + '"/><path class="fin-spark__line" d="' + line + '"/></svg>';
  }

  function renderCharts(animate) {
    chartData = readData();
    if (!chartData) return;
    document.querySelectorAll('[data-chart]').forEach(function (box) {
      try {
        var kind = box.getAttribute('data-chart');
        if (kind === 'cashflow') drawCashflow(box, chartData.cashflow, animate);
        else if (kind === 'categories') drawDonut(box, chartData.categories, animate);
        else if (kind === 'fuel') drawSpark(box, chartData.cashflow && chartData.cashflow.fuel);
      } catch (err) {
        box.innerHTML = '';
        if (window.console) console.error('Финансы: график не построен', err);
      }
    });
  }

  // Перерисовать по ширине — без повторной анимации.
  var lastWidth = window.innerWidth, resizeTimer = null;
  window.addEventListener('resize', function () {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { renderCharts(false); }, 120);
  });

  renderCharts(true);

  var auto = root.getAttribute('data-open-new');
  if (auto) openSheet(auto);
})();
