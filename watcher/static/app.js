// Watcher — one script, no build step, no framework.
// House rules: delegation via data-act (never ids) for rendered content; every
// id this file reaches for must exist in index.html (test_ui_contract enforces);
// all interpolated text goes through esc().
'use strict';

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let items = [];
let pendingDelete = null;

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

function itemRow(it) {
  const li = document.createElement('li');
  li.className = 'item' + (it.done ? ' done' : '');
  li.innerHTML = `
    <button class="tick" data-act="toggle" data-id="${it.id}" aria-label="toggle"></button>
    <span class="text">${esc(it.text)}</span>
    <button class="del" data-act="del" data-id="${it.id}" aria-label="delete">&times;</button>`;
  return li;
}

function render() {
  const list = $('#items');
  list.textContent = '';
  for (const it of items) list.appendChild(itemRow(it));
  $('#empty').hidden = items.length > 0;
}

async function refresh() {
  items = (await api('/api/items')).items;
  render();
}

function askDelete(id) {
  pendingDelete = id;
  $('#confirm-text').textContent = 'Delete this? This cannot be undone.';
  $('#confirm-overlay').hidden = false;
}

async function doDelete() {
  if (pendingDelete !== null) {
    try {
      await api('/api/items/' + pendingDelete, { method: 'DELETE' });
      toast('Deleted');
    } catch (e) { toast('Delete failed: ' + e.message); }
  }
  pendingDelete = null;
  $('#confirm-overlay').hidden = true;
  await refresh();
}

// single delegation root for everything rendered
$('#view').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  const id = Number(btn.dataset.id);
  if (act === 'toggle') {
    const it = items.find((x) => x.id === id);
    if (!it) return;
    await api('/api/items/' + id, { method: 'PUT', body: JSON.stringify({ done: !it.done }) });
    await refresh();
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
  const input = $('#new-text');
  const text = input.value.trim();
  if (!text) return;
  try {
    await api('/api/items', { method: 'POST', body: JSON.stringify({ text }) });
    input.value = '';
    toast('Added');
    await refresh();
  } catch (e) { toast('Add failed: ' + e.message); }
});

refresh().catch((e) => toast('Load failed: ' + e.message));
