// main.js — 应用入口：令牌门 + 哈希路由 + 顶栏 + 视图渲染
import { api, getToken, setToken, clearToken } from './api.js';
import { el, toast } from './ui.js';
import { store } from './store.js';
import { renderEngagementList } from './engagement/engagement-list.js';
import { renderWorkbench } from './engagement/workbench.js';

const app = document.getElementById('app');

// ---------- 哈希路由 ----------
function parseHash() {
  const h = location.hash.replace(/^#/, '') || '/';
  const parts = h.split('/').filter(Boolean);
  if (parts[0] === 'engagements') {
    if (parts[1]) return { name: 'workbench', eid: decodeURIComponent(parts[1]) };
    return { name: 'engagements' };
  }
  return { name: 'engagements' };
}

function navigate(eid) {
  location.hash = `#/engagements/${encodeURIComponent(eid)}`;
}

// ---------- 令牌门 ----------
function renderTokenGate() {
  app.replaceChildren(
    el('div', { class: 'center-screen' },
      el('div', { class: 'token-card' },
        el('h2', { text: 'Cairn v2 · 渗透测试工作台' }),
        el('p', { class: 'muted', text: '请输入 Cairn Server 访问令牌（CAIRN_API_TOKEN）。令牌仅保存在浏览器 localStorage，SSE 通道走一次性 ticket，不进入 URL。' }),
        el('input', { type: 'password', id: 'token-input', placeholder: 'Bearer token', autocomplete: 'off' }),
        el('div', {},
          el('button', {
            class: 'btn',
            onclick: async () => {
              const v = document.getElementById('token-input').value.trim();
              if (!v) return toast('请输入令牌', 'error');
              setToken(v);
              try {
                await api.get('/engagements?limit=1');
                store.setState({ lastError: null });
                render();
              } catch (e) {
                clearToken();
                toast(`令牌校验失败：${e.message}`, 'error');
              }
            },
          }, '连接'),
        ),
      ),
    ),
  );
  const input = document.getElementById('token-input');
  if (input) {
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') e.target.nextElementSibling.querySelector('button').click(); });
    setTimeout(() => input.focus(), 50);
  }
}

// ---------- 顶栏 ----------
function renderTopbar() {
  const state = store.getState();
  const cur = state.route.name === 'workbench' ? state.engagement : null;
  return el('div', { class: 'topbar' },
    el('span', { class: 'brand' }, 'Cairn ', el('span', { class: 'accent' }, 'v2')),
    el('span', { class: 'muted', text: '渗透测试工作台' }),
    el('span', { class: 'spacer' }),
    cur
      ? el('span', { class: 'act-meta' }, `${cur.id} · ${cur.title}`)
      : null,
    el('button', {
      onclick: () => { location.hash = '#/engagements'; },
    }, 'Engagements'),
    el('button', {
      onclick: () => {
        clearToken();
        store.setState({ route: { name: 'engagements' }, engagement: null });
        render();
      },
    }, '退出令牌'),
  );
}

// ---------- 渲染 ----------
let currentCleanup = null;

async function render() {
  if (currentCleanup) { try { currentCleanup(); } catch (e) { console.error(e); } currentCleanup = null; }
  const route = parseHash();
  store.setState({ route });
  if (!getToken()) { renderTokenGate(); return; }

  let body;
  if (route.name === 'engagements') {
    body = await renderEngagementList(navigate);
  } else if (route.name === 'workbench') {
    const w = await renderWorkbench(route.eid, navigate);
    body = w.node;
    currentCleanup = w.cleanup;
  } else {
    body = el('div', { class: 'empty', text: '未知路由' });
  }
  app.replaceChildren(renderTopbar(), el('div', { class: 'content' }, body));
}

window.addEventListener('hashchange', render);
window.addEventListener('cairn:auth-invalid', () => {
  // 令牌失效（401）：清令牌，回令牌门
  clearToken();
  render();
});

render();
