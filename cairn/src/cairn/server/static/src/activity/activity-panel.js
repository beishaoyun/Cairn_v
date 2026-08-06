// activity-panel.js — 任务活动面板（frontend §2；42 契约 B/C/D/E）
//  活动行三态 + 阶段点 + 最近事件文本 + 时长 + 事件计数（无百分比进度条）
//  展开行才开 SSE；活动任务 >5 其余 2s 汇总轮询 GET /engagements/{id}/tasks?active=true
//  排序：running → queued → 终态（finished_at 倒序），最多保留最近 50
import { api } from '../api.js';
import { el, toast, statusMeta, taskTypeLabel, fmtDuration, parseOutcomeNote, relativeTime, fmtTime } from '../ui.js';
import { store, bus } from '../store.js';
import { EventStream } from './event-stream.js';

const MAX_ROWS = 50;
const ACTIVE_POLL_MS = 2000;
const FULL_POLL_MS = 5000;

export class ActivityPanel {
  constructor(eid) {
    this.eid = eid;
    this.tasks = [];
    this.expanded = new Set();
    this.streams = new Map();   // taskId -> EventStream
    this.timers = [];
    this.root = null;
    this.rowsEl = null;
    this._destroyed = false;
    this._mounted = false;
  }

  render() {
    this.root = el('div', { class: 'activity-panel' });
    this.rowsEl = el('div', { class: 'activity-rows' });
    const head = el('div', { class: 'panel-head' },
      el('h3', { text: '任务活动' }),
      el('span', { class: 'muted', text: '点击行展开实时事件流（SSE，断线自动续传）' }),
      el('span', { class: 'muted' }, ' · '),
      el('a', { href: `#/engagements/${this.eid}/timeline`, text: '时间线' }),
    );
    this.root.append(head, this.rowsEl);
    return this.root;
  }

  async mount() {
    if (this._mounted) return;
    this._mounted = true;
    this._destroyed = false;
    await this.loadTasks();
    // 活动任务 >5 → 其余行 2s 汇总轮询；否则 5s 全量
    this.timers.push(setInterval(() => this.loadTasks(store.getState().activeTasks?.length > 5 ? { active: true } : undefined), ACTIVE_POLL_MS));
    this.timers.push(setInterval(() => this.loadTasks(), FULL_POLL_MS));
    // 时间线跳转 → 展开对应任务行
    this.offExpand = bus.on('activity:expand-task', (runId) => this._expand(runId));
  }

  unmount() {
    this._mounted = false;
    this._destroyed = true;
    this.timers.forEach(clearInterval);
    this.timers = [];
    for (const stream of this.streams.values()) stream.unmount();
    this.streams.clear();
    this.offExpand?.();
    this.offExpand = null;
  }

  _expand(taskId) {
    if (!this.expanded.has(taskId)) {
      this.expanded.add(taskId);
      this._renderRows();
    }
  }

  async loadTasks(opts) {
    if (this._destroyed) return;
    try {
      const q = opts?.active ? '?active=true' : '?limit=100';
      const tasks = await api.get(`/engagements/${this.eid}/tasks${q}`);
      if (opts?.active) {
        store.setState({ activeTasks: tasks });
      } else {
        const merged = this.merge(tasks);
        store.setState({ tasks: merged });
        this._emitTerminals(merged);
        this.tasks = merged;
        this._renderRows();
      }
    } catch (e) { /* 静默 */ }
  }

  // 业务联动信号：任务由非终态 → 终态（explore success → 热力图刷新；verify 终态 → findings 刷新）
  _emitTerminals(merged) {
    const prevMap = new Map(this.tasks.map((t) => [t.id, t]));
    for (const t of merged) {
      const prev = prevMap.get(t.id);
      const terminal = ['success', 'failed', 'cancelled', 'unhealthy', 'rejected'].includes(t.status);
      if (terminal && prev && !['success', 'failed', 'cancelled', 'unhealthy', 'rejected'].includes(prev.status)) {
        bus.emit('activity:task-terminal', t);
      }
    }
  }

  // 增量合并：以服务端为准，保留展开行的 stream
  merge(incoming) {
    const map = new Map();
    for (const t of this.tasks) map.set(t.id, t);
    for (const t of incoming) map.set(t.id, t);
    let arr = [...map.values()];
    arr.sort(sortTasks);
    if (arr.length > MAX_ROWS) arr = arr.slice(0, MAX_ROWS);
    return arr;
  }

  _renderRows() {
    if (!this.rowsEl) return;
    const rows = this.tasks.map((t) => this._row(t));
    this.rowsEl.replaceChildren(...rows);
    // 清理已不在列表中的展开流（如被 50 上限挤出）
    const ids = new Set(this.tasks.map((t) => t.id));
    for (const [tid, stream] of this.streams) {
      if (!ids.has(tid)) { stream.unmount(); this.streams.delete(tid); }
    }
  }

  // 活动行（折叠态默认）：状态徽标 + 类型标签 + worker + 业务标签 + 时长 + 事件计数 + 最近事件
  _row(task) {
    const st = statusMeta(task.status);
    const isExpanded = this.expanded.has(task.id);
    const biz = parseOutcomeNote(task.outcome_note);
    const latest = task.latest_event;
    const latestText = latest ? `[${latest.kind}] ${latest.message || ''}` : (biz.text || '—');

    const head = el('div', { class: 'act-head' },
      el('span', { class: `status-badge st-${task.status}` },
        el('span', { class: `status-dot ${task.status === 'running' ? 'running' : ''}`, style: dotColor(task.status) }),
        `${st.icon} ${st.text}`,
      ),
      el('span', { class: `type-tag tt-${task.task_type}` }, taskTypeLabel(task.task_type) + retestSuffix(biz)),
      el('span', { class: 'act-meta', text: task.worker || '—' }),
      biz.finding_id ? el('span', { class: 'biz-tag' }, `finding ${biz.finding_id}`) : null,
      biz.coverage_item_ids?.length ? el('span', { class: 'biz-tag' }, biz.coverage_item_ids.join(', ')) : null,
      el('span', { class: 'act-meta', text: `⏱ ${fmtDuration(task.duration_seconds)}` }),
      el('span', { class: 'act-meta', text: `${task.event_count ?? 0} evt` }),
      el('span', { class: 'act-recent', title: latestText }, latestText),
      el('span', { class: 'act-expand', text: isExpanded ? '▾' : '▸' }),
    );

    const row = el('div', { class: 'act-row' }, head);
    if (!isExpanded) {
      row.classList.add('summary');
      head.addEventListener('click', () => this._toggle(task.id));
      return row;
    }

    // 展开态：事件流容器
    const body = el('div', { class: 'act-body' });
    head.addEventListener('click', () => this._toggle(task.id));
    row.append(body);
    this._attachStream(task.id, body);
    return row;
  }

  _toggle(taskId) {
    if (this.expanded.has(taskId)) {
      this.expanded.delete(taskId);
      this.streams.get(taskId)?.unmount();
      this.streams.delete(taskId);
    } else {
      this.expanded.add(taskId);
    }
    this._renderRows();
  }

  _attachStream(taskId, body) {
    // 连接数控制：同一任务只开一条流（重渲染时把既有容器移回新 body）
    if (this.streams.has(taskId)) {
      const existing = this.streams.get(taskId);
      if (existing.container && !body.contains(existing.container)) body.append(existing.container);
      return;
    }
    const task = this.tasks.find((t) => t.id === taskId);
    const live = !!task && ['queued', 'running'].includes(task.status);
    const stream = new EventStream(taskId, {
      live,
      onSeq: () => {
        // 事件计数随流更新（不重排全表，避免闪烁）
      },
    });
    this.streams.set(taskId, stream);
    body.append(stream.render());
    stream.mount();
  }
}

function sortTasks(a, b) {
  const rank = (t) => (t.status === 'running' ? 0 : t.status === 'queued' ? 1 : 2);
  const r = rank(a) - rank(b);
  if (r !== 0) return r;
  const ta = a.finished_at || a.started_at || '';
  const tb = b.finished_at || b.started_at || '';
  return tb.localeCompare(ta);
}

function dotColor(status) {
  const map = {
    queued: 'var(--gray)', running: 'var(--blue)', success: 'var(--green)',
    failed: 'var(--red)', cancelled: 'var(--gray)', unhealthy: 'var(--orange)', rejected: 'var(--red)',
  };
  return `background:${map[status] || 'var(--gray)'}`;
}

function retestSuffix(biz) {
  if (biz.retest_round) return ` ⤾${biz.retest_round}`;
  return '';
}
