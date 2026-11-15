// Watcher — one script, no build step, no framework.
// House rules: delegation via data-act (never ids) for rendered content; every
// id this file reaches for must exist in index.html (test_ui_contract enforces);
// all interpolated text goes through esc().
'use strict';

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let watches = [];
let defaultInterval = 1440;
let pendingDelete = null;
let expandedId = null;
const historyCache = {};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}

let toastTimer = 0;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function fmtInterval(min) {
  if (min % 1440 === 0) return (min / 1440 === 1 ? 'daily' : `every ${min / 1440} days`);
  if (min % 60 === 0) return (min === 60 ? 'hourly' : `every ${min / 60} hours`);
  return `every ${min} min`;
}

function fmtTime(iso) {
  if (!iso) return 'never';
  const d = new Date(iso + 'Z');
  return isNaN(d) ? iso : d.toLocaleString();
}

function statusGlyph(w) {
  if (w.status === 'pending') return '…';
  if (w.status === 'needs_review') return '!';
  if (w.status === 'paused') return '⏸';
  if (w.last_status === 'changed') return '▲';
  if (w.last_status === 'error' || w.last_status === 'blocked') return '✗';
  if (w.last_status === 'challenged') return '?';
  return '●';
}

function watchRow(w) {
  const li = document.createElement('li');
  const cls = ['watch', w.status];
  if (w.last_status === 'changed') cls.push('triggered');
  li.className = cls.join(' ');
  const detail = esc(w.last_detail || w.onboard_error || 'not checked yet');
  li.innerHTML = `
    <div class="w-main">
      <span class="glyph" title="${esc(w.status)}/${esc(w.last_status || '')}">${statusGlyph(w)}</span>
      <div class="w-body">
        <a class="w-url" href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">${esc(w.url)}</a>
        <p class="w-intent">${esc(w.intent)}</p>
        <p class="w-meta muted">${fmtInterval(w.interval_minutes)} · last: ${esc(fmtTime(w.last_check_at))} · ${detail}</p>
      </div>
      <div class="w-acts">
        <button data-act="check" data-id="${w.id}" title="check now">⟳</button>
        <button data-act="reonboard" data-id="${w.id}" title="re-fit the detector">✎</button>
        ${w.status === 'paused'
          ? `<button data-act="resume" data-id="${w.id}" title="resume">▶</button>`
          : `<button data-act="pause" data-id="${w.id}" title="pause">⏸</button>`}
        <button data-act="history" data-id="${w.id}" title="history">…</button>
        <button data-act="del" data-id="${w.id}" class="del" title="delete">&times;</button>
      </div>
    </div>
    ${w.status === 'ready' && w.spec && w.spec.notes
      ? `<p class="w-notes muted">detector: ${esc(w.spec.notes)}</p>` : ''}
    ${expandedId === w.id
      ? `<div class="w-history" data-history-for="${w.id}">${historyHtml(w.id)}</div>` : ''}`;
  return li;
}

function historyHtml(id) {
  const rows = historyCache[id];
  if (!rows) return '<p class="muted">loading…</p>';
  if (!rows.length) return '<p class="muted">no checks yet</p>';
  return rows.slice(0, 20).map((c) =>
    `<p class="h-row"><span class="h-status s-${esc(c.status)}">${esc(c.status)}</span>` +
    ` <span class="muted">${esc(fmtTime(c.ts))}${c.rung !== null && c.rung !== undefined ? ` · rung ${c.rung}` : ''}</span>` +
    ` ${esc(c.detail || '')}</p>`).join('');
}

function render() {
  const list = $('#watches');
  list.textContent = '';
  for (const w of watches) list.appendChild(watchRow(w));
  $('#empty').hidden = watches.length > 0;
}

async function refresh() {
  const data = await api('/api/watches');
  watches = data.watches;
  defaultInterval = data.default_interval_minutes || 1440;
  render();
}

async function loadHistory(id) {
  historyCache[id] = null;                       // mark loading
  render();
  try {
    historyCache[id] = (await api(`/api/watches/${id}/checks`)).checks;
  } catch (e) {
    historyCache[id] = [];
  }
  render();
}

function askDelete(id) {
  pendingDelete = id;
  $('#confirm-text').textContent = 'Delete this watch and its check history? This cannot be undone.';
  $('#confirm-overlay').hidden = false;
}

async function doDelete() {
  if (pendingDelete !== null) {
    try {
      await api('/api/watches/' + pendingDelete, { method: 'DELETE' });
      toast('Deleted');
    } catch (e) { toast('Delete failed: ' + e.message); }
  }
  pendingDelete = null;
  $('#confirm-overlay').hidden = true;
  await refresh();
}

async function patchWatch(id, fields, okMsg) {
  try {
    await api('/api/watches/' + id, { method: 'PUT', body: JSON.stringify(fields) });
    if (okMsg) toast(okMsg);
    await refresh();
  } catch (e) { toast('Failed: ' + e.message); }
}

async function postWatch(id, path, okMsg) {
  try {
    await api(`/api/watches/${id}/${path}`, { method: 'POST' });
    if (okMsg) toast(okMsg);
    await refresh();
  } catch (e) { toast('Failed: ' + e.message); }
}

// single delegation root for everything rendered
$('#view').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  const id = Number(btn.dataset.id);
  if (act === 'check') {
    btn.textContent = '…';
    await postWatch(id, 'check', 'Checked');
  } else if (act === 'reonboard') {
    await postWatch(id, 'reonboard', 'Re-fitting detector…');
  } else if (act === 'pause') {
    await patchWatch(id, { status: 'paused' }, 'Paused');
  } else if (act === 'resume') {
    await patchWatch(id, { status: 'ready' }, 'Resumed');
  } else if (act === 'history') {
    expandedId = (expandedId === id) ? null : id;
    if (expandedId === id) await loadHistory(id); else render();
  } else if (act === 'del') {
    askDelete(id);
  }
});

$('#confirm-overlay').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (btn && btn.dataset.act === 'confirm-yes') return doDelete();
  if (btn && btn.dataset.act === 'confirm-no') { pendingDelete = null; $('#confirm-overlay').hidden = true; return; }
  if (ev.target === $('#confirm-overlay')) { pendingDelete = null; $('#confirm-overlay').hidden = true; }
});

$('#add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const url = $('#new-url').value.trim();
  const intent = $('#new-intent').value.trim();
  if (!url || !intent) { toast('URL and intent are both required'); return; }
  try {
    await api('/api/watches', {
      method: 'POST',
      body: JSON.stringify({
        url, intent,
        interval_minutes: Number($('#new-interval').value),
        notify_email: $('#new-notify').checked,
      }),
    });
    $('#new-url').value = '';
    $('#new-intent').value = '';
    toast('Watch added — fitting detector…');
    await refresh();
  } catch (e) { toast('Add failed: ' + e.message); }
});

// ---- settings ---------------------------------------------------------------

async function openSettings() {
  try {
    const s = (await api('/api/settings')).settings;
    $('#set-llm-url').value = s.llm.base_url || '';
    $('#set-llm-model').value = s.llm.model || '';
    $('#set-email-enabled').checked = !!s.email.enabled;
    $('#set-email-to').value = s.email.to || '';
    $('#set-email-from').value = s.email.from_addr || '';
    $('#set-default-interval').value = String(s.scheduler.default_interval_minutes || 1440);
    $('#settings-overlay').hidden = false;
  } catch (e) { toast('Settings load failed: ' + e.message); }
}

$('#topbar').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (btn && btn.dataset.act === 'open-settings') openSettings();
});

$('#settings-overlay').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (btn && btn.dataset.act === 'close-settings') { $('#settings-overlay').hidden = true; return; }
  if (btn && btn.dataset.act === 'save-settings') {
    const body = { settings: {
      llm: {
        base_url: $('#set-llm-url').value.trim(),
        model: $('#set-llm-model').value.trim(),
      },
      email: {
        enabled: $('#set-email-enabled').checked,
        to: $('#set-email-to').value.trim(),
        from_addr: $('#set-email-from').value.trim(),
      },
      scheduler: { default_interval_minutes: Number($('#set-default-interval').value) },
    } };
    const label = btn.textContent;
    btn.textContent = '…';
    try {
      await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
      toast('Settings saved');
      $('#settings-overlay').hidden = true;
    } catch (e) { toast('Save failed: ' + e.message); }
    btn.textContent = label;
    return;
  }
  if (ev.target === $('#settings-overlay')) $('#settings-overlay').hidden = true;
});

refresh().catch((e) => toast('Load failed: ' + e.message));
setInterval(() => { refresh().catch(() => {}); }, 3000);   // dashboard poll
