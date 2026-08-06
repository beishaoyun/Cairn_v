// event-stream.js — 事件流渲染（frontend §3）
// 六类着色 / 摘要≤120 字符 + 原始分片懒加载 / command 等宽+复制+「命令+回显」折叠
// 虚拟滚动（可视区 ×3）/ 自动滚动 + 回到底部 / 增量合并（先历史 50 再 SSE，按 seq 去重）
import { api } from '../api.js';
import { el, esc, fmtTime, copyText, toast, eventKindMeta, absUrl } from '../ui.js';
import { connectTaskStream } from '../sse.js';

const HISTORY_LIMIT = 50;
const ROW_H = 24;            // 虚拟滚动估计行高
const VIRT_THRESHOLD = 300;  // 超过则启用虚拟滚动
const PREVIEW = 120;         // 摘要前 120 字符

export class EventStream {
  /**
   * @param {string} runId
   * @param {object} opts
   * @param {(lastSeq:number)=>void} [opts.onSeq] 更新面板行事件计数
   */
  constructor(runId, opts = {}) {
    this.runId = runId;
    this.live = opts.live !== false; // false = 终态任务只拉历史，不长期占连接
    this.onSeq = opts.onSeq;
    this.events = new Map();      // seq -> event
    this.maxSeq = 0;
    this.conn = null;
    this.container = null;
    this.scrollEl = null;
    this.virtual = false;
    this._renderRange = { start: 0, end: 0 };
    this._rawCache = new Map();   // seq -> Promise<string>
    this._rawText = new Map();    // seq -> resolved text（懒加载后持久显示）
    this._destroyed = false;
  }

  render() {
    this.container = el('div', { class: 'event-stream', 'data-run': this.runId });
    this.scrollEl = this.container;
    this._bottomBtn = el('div', { class: 'stream-ctrl' },
      el('button', {
        class: 'back-to-bottom',
        style: 'display:none',
        onclick: () => this.scrollToBottom(),
      }, '回到底部'),
    );
    this.container.append(this._bottomBtn);
    this.container.addEventListener('scroll', () => this._onScroll());
    return this.container;
  }

  async mount() {
    // 1. 拉最近历史：先取 event_count，再拉尾部 limit=50（长任务避免从 seq=1 开始）
    try {
      const detail = await api.get(`/tasks/${this.runId}`);
      const count = Number(detail.event_count || 0);
      const after = Math.max(0, count - HISTORY_LIMIT);
      const data = await api.get(`/tasks/${this.runId}/events?after_seq=${after}&limit=${HISTORY_LIMIT}`);
      for (const ev of data.items || []) this._add(ev);
    } catch (e) {
      toast(`历史事件加载失败：${e.message}`, 'error');
    }
    if (this._destroyed) return;
    this._paint();
    this.scrollToBottom();
    if (!this.live) return; // 终态任务：仅历史，不占连接
    // 2. 开实时流（SSE 优先，断线新 ticket + after_seq 续传；受限降级长轮询）
    this.conn = connectTaskStream(this.runId, {
      afterSeq: this.maxSeq,
      onEvent: (ev) => this._add(ev),
      onProgress: (lastSeq) => this.onSeq?.(lastSeq),
      onMode: (mode, why) => {
        const tag = this.container.querySelector('.stream-mode');
        if (tag) tag.textContent = mode === 'sse' ? 'SSE 实时' : `长轮询${why ? `（${why}）` : ''}`;
      },
      onError: () => { /* 已降级处理 */ },
    });
  }

  unmount() {
    this._destroyed = true;
    this.conn?.close();
  }

  // ---------- 数据合并（按 seq 去重） ----------
  _add(ev) {
    const seq = Number(ev.seq);
    if (!Number.isFinite(seq)) return;
    if (this.events.has(seq)) return;
    this.events.set(seq, ev);
    if (seq > this.maxSeq) this.maxSeq = seq;
    this.onSeq?.(this.maxSeq);
    if (this._destroyed) return;
    // 更新虚拟滚动可视区（若在底部则重绘 + 跟随）
    const atBottom = this._atBottom();
    if (!this.virtual) {
      this._paint();
    } else {
      this._paintRange();
      if (atBottom) this.scrollToBottom();
    }
  }

  // ---------- 渲染 ----------
  _eventsSorted() {
    return [...this.events.values()].sort((a, b) => a.seq - b.seq);
  }

  _atBottom() {
    if (!this.scrollEl) return true;
    const d = this.scrollEl;
    return d.scrollTop + d.clientHeight >= d.scrollHeight - 40;
  }

  scrollToBottom() {
    requestAnimationFrame(() => {
      if (this.scrollEl) this.scrollEl.scrollTop = this.scrollEl.scrollHeight;
      this._bottomBtn.style.display = 'none';
    });
  }

  _onScroll() {
    if (!this.scrollEl) return;
    const d = this.scrollEl;
    const nearBottom = d.scrollTop + d.clientHeight >= d.scrollHeight - 40;
    this._bottomBtn.style.display = nearBottom ? 'none' : 'inline-block';
    if (this.virtual) this._paintRange();
  }

  _paint() {
    const sorted = this._eventsSorted();
    this.virtual = sorted.length > VIRT_THRESHOLD;
    if (this.virtual) {
      this._paintRange(sorted);
    } else {
      this._renderAll(sorted);
    }
  }

  _paintRange(sorted) {
    sorted = sorted || this._eventsSorted();
    const d = this.scrollEl;
    const viewH = d.clientHeight || 300;
    const scrollTop = d.scrollTop || 0;
    const pad = Math.max(4, Math.round(viewH / ROW_H));
    const start = Math.max(0, Math.floor(scrollTop / ROW_H) - 1 - pad);
    const end = Math.min(sorted.length, Math.ceil((scrollTop + viewH) / ROW_H) + 1 + pad);
    if (start === this._renderRange.start && end === this._renderRange.end) return;
    this._renderRange = { start, end };
    const inner = this._buildLines(sorted.slice(start, end));
    // 占位行撑高滚动容器，保持滚动位置（可视区 ×3）
    const padTop = el('div', { style: `height:${start * ROW_H}px` });
    const padBottom = el('div', { style: `height:${(sorted.length - end) * ROW_H}px` });
    this.scrollEl.replaceChildren(padTop, ...inner, padBottom, this._bottomBtn);
  }

  _renderAll(sorted) {
    const inner = this._buildLines(sorted);
    this.scrollEl.replaceChildren(...inner, this._bottomBtn);
  }

  _buildLines(events) {
    const out = [];
    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      out.push(this._line(ev, i + 1));
    }
    return out;
  }

  _line(ev, lineNo) {
    const kind = eventKindMeta(ev.kind);
    const lv = ev.level || 'info';
    const time = fmtTime(ev.ts);
    let content;
    if (ev.kind === 'command') {
      content = this._commandLine(ev, lineNo);
    } else {
      const rawLoaded = this._rawText.has(ev.seq);
      const text = rawLoaded ? this._rawText.get(ev.seq) : this._preview(ev);
      content = el('span', { class: `event-msg ${rawLoaded ? '' : this._isTruncated(ev) ? 'truncated' : ''}` }, text);
      if (this._isTruncated(ev) || ev.raw_path) {
        content.append(this._rawLink(ev, rawLoaded));
      }
    }
    return el('div', {
      class: `event-line lv-${lv}`,
      dataset: { seq: ev.seq, line: lineNo },
      'data-seq': ev.seq,
    },
      el('span', { class: 'event-time', text: time }),
      el('span', { class: `event-kind ${kind.cls}`, text: kind.icon }),
      el('span', { class: `event-level lv-${lv}`, text: lv }),
      content,
    );
  }

  _preview(ev) {
    const msg = ev.message || '';
    return msg.length > PREVIEW ? msg.slice(0, PREVIEW) + ' …' : msg;
  }

  _isTruncated(ev) {
    return (ev.message || '').length > PREVIEW || !!ev.raw_path;
  }

  _rawLink(ev, loaded) {
    const link = el('span', {
      class: 'raw-link',
      onclick: async (e) => {
        e.stopPropagation();
        if (loaded) return; // 已展开
        await this._loadRaw(ev);
      },
    }, loaded ? '收起' : '原始日志');
    return link;
  }

  async _loadRaw(ev) {
    if (!this._rawCache.has(ev.seq)) {
      this._rawCache.set(ev.seq, api.get(`/tasks/${this.runId}/events/${ev.seq}/raw`, { raw: true })
        .catch((err) => `[原始日志不可用] ${err.message}`));
    }
    const raw = await this._rawCache.get(ev.seq);
    this._rawText.set(ev.seq, raw);
    this._paint();
  }

  _commandLine(ev, lineNo) {
    const cmdText = ev.message || '';
    const wrapper = el('span', { class: 'event-msg' });
    const cmdSpan = el('span', { class: 'cmd-line' }, cmdText);
    const copy = el('span', {
      class: 'cmd-copy',
      onclick: async (e) => {
        e.stopPropagation();
        await copyText(cmdText);
        toast('命令已复制', 'success');
      },
    }, '复制');
    wrapper.append(cmdSpan, copy);
    // 「命令+回显」折叠：收集紧随其后的 output
    const outputs = [];
    const keys = [...this.events.keys()].sort((a, b) => a - b);
    const idx = keys.indexOf(ev.seq);
    for (let j = idx + 1; j < keys.length; j++) {
      const nxt = this.events.get(keys[j]);
      if (nxt.kind === 'output') outputs.push(nxt);
      else break;
    }
    if (outputs.length) {
      const fold = el('span', {
        class: 'cmd-fold',
        onclick: (e) => {
          e.stopPropagation();
          const body = wrapper.querySelector('.cmd-body');
          if (!body) return;
          const hidden = body.style.display === 'none';
          body.style.display = hidden ? 'block' : 'none';
          e.target.textContent = hidden ? '隐藏回显' : `回显 ${outputs.length} 行`;
        },
      }, `回显 ${outputs.length} 行`);
      wrapper.append(fold);
      // 默认展开「命令+回显」对（证据取证路径可见）；点击「隐藏回显」折叠
      const body = el('span', { class: 'cmd-body', style: 'display:block' });
      for (const o of outputs) {
        body.append(el('div', { class: 'k-output' }, this._preview(o)));
      }
      wrapper.append(body);
      fold.textContent = '隐藏回显';
    }
    return wrapper;
  }
}
