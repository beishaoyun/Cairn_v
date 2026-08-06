// sse.js — 实时事件通道接线（frontend-progress-view-design §3.3 / 24 交接物 §4）
//
// 契约要点：
// 1. 一次性 ticket：POST /tasks/{id}/events/ticket → {ticket, expires_in:5}
//    → EventSource(`/tasks/{id}/events?ticket=&after_seq=&mode=sse`)。
//    绝不把 Bearer token 放 URL（事件端点已豁免中间件，SSE 用 ticket 鉴权）。
// 2. 断线重连：ticket 一次性（消费即删），EventSource 原生自动重连会带旧 ticket →
//    422。因此 onerror 时必须 close() 原生流，用【新 ticket + after_seq 续传】手动重连，
//    指数退避（0.5s 起，×2，上限 15s）。
// 3. after_seq 续传：先拉历史（events?limit=50&after_seq=0），再开流，last_seq 递增去重。
// 4. 降级长轮询：SSE 建连失败 / 受限时走 GET .../events?mode=longpoll&after_seq= ，
//    服务端 hold ≤20s，返回 {items, last_seq}。poll 间隔 = 返回即续。
// 5. 一次连接一个任务 run；连接数控制由 activity-panel 决定何时调用 connectTaskStream。

import { api, getToken, API_BASE } from './api.js';

const KIND_EVENTS = ['step', 'tool', 'command', 'output', 'status', 'error'];
const BACKOFF_BASE = 500;
const BACKOFF_MAX = 15000;

function eventUrl(runId, ticket, afterSeq) {
  return `${API_BASE}/tasks/${runId}/events?ticket=${encodeURIComponent(ticket)}&after_seq=${afterSeq}&mode=sse`;
}

/**
 * 打开一条任务 run 的实时流。
 * @param {string} runId
 * @param {object} opts
 * @param {number} opts.afterSeq         起始断点（seq > afterSeq）
 * @param {(ev: object) => void} opts.onEvent   收到增量事件（含 kind/seq/ts/message…）
 * @param {(lastSeq: number) => void} opts.onProgress 任意事件到达（用于 last_seq 记账）
 * @param {(mode: 'sse'|'longpoll', why?: string) => void} opts.onMode 通道模式变更
 * @param {() => void} opts.onEnd         连接最终停止
 * @param {(error: Error) => void} [opts.onError]
 * @returns {{ close: () => void }}  控制器（视图卸载时调用）
 */
export function connectTaskStream(runId, opts) {
  let closed = false;
  let lastSeq = opts.afterSeq || 0;
  let attempt = 0;
  let es = null;
  let pollTimer = null;
  let pollAbort = null;
  let mode = null;

  function handleEvent(ev) {
    if (!ev || typeof ev !== 'object') return;
    const seq = Number(ev.seq ?? ev.id);
    if (Number.isFinite(seq) && seq > lastSeq) lastSeq = seq;
    opts.onEvent?.(ev);
    opts.onProgress?.(lastSeq);
  }

  function setMode(m, why) {
    if (mode !== m) {
      mode = m;
      opts.onMode?.(m, why);
    }
  }

  // ---------- SSE ----------
  async function openSSE() {
    if (closed) return;
    let ticket;
    try {
      const r = await api.post(`/tasks/${runId}/events/ticket`, {});
      ticket = r.ticket;
    } catch (e) {
      // 无法签发 ticket（如后端无此端点）→ 直接降级长轮询
      setMode('longpoll', 'ticket-issue-failed');
      scheduleLongpoll();
      return;
    }
    if (closed) return;
    try {
      es = new EventSource(eventUrl(runId, ticket, lastSeq));
    } catch (e) {
      setMode('longpoll', 'eventsource-unsupported');
      scheduleLongpoll();
      return;
    }
    let gotAny = false;
    const openHandler = () => {
      gotAny = true;
      attempt = 0; // 建连成功 → 退避重置
      setMode('sse');
    };
    es.addEventListener('open', openHandler);
    const onMsg = (evt) => {
      gotAny = true;
      try {
        const data = JSON.parse(evt.data);
        handleEvent(data);
      } catch (e) { /* 坏帧跳过 */ }
    };
    KIND_EVENTS.forEach((k) => es.addEventListener(k, onMsg));
    // 心跳注释帧不触发事件；message 兜底（data 无 event 字段时）
    es.onmessage = onMsg;
    es.onerror = () => {
      es?.close();
      es = null;
      if (closed) return;
      if (!gotAny) {
        // 首连即失败 → 判定 SSE 受限，降级长轮询
        setMode('longpoll', 'sse-open-failed');
        scheduleLongpoll();
        return;
      }
      // 中途断开 → 指数退避 + 新 ticket 重连
      attempt += 1;
      const delay = Math.min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_MAX);
      setTimeout(() => { if (!closed) openSSE(); }, delay);
    };
  }

  // ---------- 长轮询 ----------
  async function pollOnce() {
    if (closed) return;
    pollAbort = new AbortController();
    try {
      const r = await fetch(
        `${API_BASE}/tasks/${runId}/events?mode=longpoll&after_seq=${lastSeq}&limit=500`,
        { headers: { Accept: 'application/json', Authorization: getToken() ? `Bearer ${getToken()}` : '' }, signal: pollAbort.signal },
      );
      if (r.status === 401) { window.dispatchEvent(new CustomEvent('cairn:auth-invalid')); return; }
      if (!r.ok) return;
      const data = await r.json();
      const items = data?.items || [];
      for (const ev of items) handleEvent(ev);
      if (!closed) pollTimer = setTimeout(pollOnce, 0);
    } catch (e) {
      if (closed || e.name === 'AbortError') return;
      // 网络抖动 → 退避后继续长轮询
      attempt += 1;
      const delay = Math.min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_MAX);
      if (!closed) pollTimer = setTimeout(pollOnce, delay);
    }
  }

  function scheduleLongpoll() {
    if (closed) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollOnce, 0);
  }

  // 启动：优先 SSE
  setMode('sse', 'connecting');
  openSSE();

  return {
    close() {
      closed = true;
      try { es?.close(); } catch { /* noop */ }
      if (pollTimer) clearTimeout(pollTimer);
      pollAbort?.abort();
      opts.onEnd?.();
    },
    get lastSeq() { return lastSeq; },
  };
}
