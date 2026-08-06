// engagement-list.js — Engagement 选择页（GET /engagements）
import { api } from '../api.js';
import { el, fmtClock } from '../ui.js';

const STATUS_LABEL = {
  planning: '规划', active: '活动', paused: '暂停', completed: '完成', archived: '归档',
};
const STATUS_ICON = {
  planning: '○', active: '●', paused: '⏸', completed: '✓', archived: '⊘',
};

export async function renderEngagementList(navigate) {
  let engagements = [];
  let err = null;
  try {
    engagements = await api.get('/engagements?limit=100');
  } catch (e) {
    err = e.message;
  }
  const list = el('div', { class: 'list-card' },
    el('h2', { text: 'Engagements' }),
    el('p', { class: 'muted', text: '选择授权渗透测试任务进入工作台（进度面板默认展开）。' }),
  );
  if (err) {
    list.append(el('div', { class: 'alert-banner', text: `加载失败：${err}` }));
  } else if (!engagements.length) {
    list.append(el('div', { class: 'empty', text: '暂无 Engagement（请先经服务端创建，如 POST /engagements）' }));
  } else {
    const ul = el('ul', { class: 'eng-list' });
    for (const eng of engagements) {
      const st = STATUS_ICON[eng.status] || '?';
      ul.append(
        el('li', {
          onclick: () => navigate(eng.id),
        },
          el('div', { class: 'title' }, `${st} ${eng.title}`),
          el('div', { class: 'meta' },
            `${eng.id} · ${STATUS_LABEL[eng.status] || eng.status} · 窗口 ${fmtClock(eng.authorized_start_at)} – ${fmtClock(eng.authorized_end_at)}`
            + (eng.completed_at ? ` · 完成 ${fmtClock(eng.completed_at)}` : ''),
          ),
        ),
      );
    }
    list.append(ul);
  }
  return el('div', { class: 'center-screen' }, list);
}
