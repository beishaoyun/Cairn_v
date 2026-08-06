// timeline-view.js — 统一时间线（D3 轻量实现，无外部库；capture spec §7.4 / frontend §1.2）
//  六源（graph/task/finding/traffic/coverage/report）着色 + 类型过滤 + 增量续拉 after_ts + 点击跳转源详情
import { api } from '../api.js';
import { el, fmtClock, toast } from '../ui.js';
import { bus } from '../store.js';

const SOURCES = [
  { id: 'graph', label: '图' },
  { id: 'task', label: '任务' },
  { id: 'finding', label: '漏洞' },
  { id: 'traffic', label: '流量' },
  { id: 'coverage', label: '覆盖' },
  { id: 'report', label: '报告' },
];
const PAGE = 200;

export class TimelineView {
  constructor(eid) {
    this.eid = eid;
    this.items = [];
    this.enabled = new Set(SOURCES.map((s) => s.id));
    this.lastTs = null;
    this.root = null;
    this.timer = null;
    this._destroyed = false;
  }

  render() {
    this.root = el('div', { class: 'timeline-view' });
    this.root.innerHTML = '<div class="loading"><span class="spin"></span> 加载时间线…</div>';
    return this.root;
  }

  async mount() {
    await this.load();
    this.timer = setInterval(() => this.refresh(), 10000);
  }

  unmount() {
    this._destroyed = true;
    clearInterval(this.timer);
  }

  async load(reset = true) {
    try {
      const q = reset ? `?limit=${PAGE}` : `?after_ts=${encodeURIComponent(this.lastTs || '')}&limit=${PAGE}`;
      const items = await api.get(`/engagements/${this.eid}/timeline${q}`);
      if (reset) {
        this.items = items;
      } else {
        const seen = new Set(this.items.map((i) => `${i.source}:${i.ref}:${i.ts}`));
        for (const it of items) {
          const key = `${it.source}:${it.ref}:${it.ts}`;
          if (!seen.has(key)) { this.items.push(it); seen.add(key); }
        }
      }
      if (items.length) {
        this.lastTs = items[items.length - 1].ts;
      }
      if (!this._destroyed) this._render();
    } catch (e) {
      if (!this._destroyed) this.root.replaceChildren(el('div', { class: 'alert-banner', text: `时间线加载失败：${e.message}` }));
    }
  }

  refresh() {
    // 增量续拉：仅取比当前 lastTs 新的（避免重复）
    return this.load(false);
  }

  _render() {
    const filtered = this.items.filter((i) => this.enabled.has(i.source));
    const filterRow = el('div', { class: 'timeline-filters' },
      ...SOURCES.map((s) => {
        const on = this.enabled.has(s.id);
        const cb = el('input', { type: 'checkbox', checked: on, onchange: () => { this.enabled.has(s.id) ? this.enabled.delete(s.id) : this.enabled.add(s.id); this._render(); } });
        return el('label', { class: 'lg', style: `color:${sourceColor(s.id)};cursor:pointer` }, cb, s.label);
      }),
      el('button', { class: 'btn ghost small', onclick: () => { this.enabled = new Set(SOURCES.map((x) => x.id)); this._render(); } }, '全选'),
      el('span', { class: 'spacer', style: 'flex:1' }),
      el('span', { class: 'muted' }, `${filtered.length} 条`),
    );

    const list = el('div');
    if (!filtered.length) list.append(el('div', { class: 'empty', text: '暂无时间线事件' }));
    for (const it of filtered) {
      list.append(this._item(it));
    }
    const more = el('div', { class: 'tl-loadmore' },
      el('button', { onclick: () => this.load(false) }, '加载更多'),
    );

    this.root.replaceChildren(filterRow, list, more);
  }

  _item(it) {
    const node = el('div', {
      class: `tl-item s-${it.source}`,
      onclick: () => this._jump(it),
    },
      el('span', { class: 'tl-source', text: sourceLabel(it.source) }),
      el('span', { class: 'tl-time', text: fmtClock(it.ts) }),
      el('span', { class: 'tl-summary' },
        it.kind ? el('span', { class: 'biz-tag', text: it.kind }) : null,
        ' ',
        String(it.summary || ''),
      ),
      el('span', { class: 'tl-ref', text: it.actor ? `${it.actor} · ` : '' + (it.ref || '') }),
    );
    return node;
  }

  _jump(it) {
    switch (it.source) {
      case 'task':
        bus.emit('workbench:switch-tab', 'activity');
        if (it.task_run_id) setTimeout(() => bus.emit('activity:expand-task', it.task_run_id), 100);
        break;
      case 'finding':
        bus.emit('workbench:switch-tab', 'findings');
        break;
      case 'coverage':
        bus.emit('workbench:switch-tab', 'heatmap');
        break;
      case 'graph':
        bus.emit('workbench:switch-tab', 'graph');
        break;
      case 'report':
        bus.emit('workbench:switch-tab', 'report');
        break;
      case 'traffic':
        toast(`流量 ${it.ref}：${it.summary || ''}`, 'info');
        break;
    }
  }
}

function sourceColor(s) {
  return { graph: 'var(--purple)', task: 'var(--blue)', finding: 'var(--orange)', traffic: 'var(--green)', coverage: 'var(--amber)', report: 'var(--gray)' }[s] || 'var(--text)';
}
function sourceLabel(s) {
  return SOURCES.find((x) => x.id === s)?.label || s;
}
