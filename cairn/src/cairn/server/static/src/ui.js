// ui.js — DOM 辅助 + 格式化 + toast（事件原文（英文 CLI 输出）保留不翻译）
import { API_BASE } from './api.js';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v !== undefined && v !== null && v !== false) {
      node.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

export function fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch { return String(iso); }
}

export function fmtClock(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch { return String(iso); }
}

export function relativeTime(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return `${Math.round(diff)}s 前`;
  if (diff < 3600) return `${Math.round(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h 前`;
  return `${Math.round(diff / 86400)}d 前`;
}

export function toast(message, kind = 'info') {
  let wrap = document.querySelector('.toast-wrap');
  if (!wrap) {
    wrap = el('div', { class: 'toast-wrap' });
    document.body.append(wrap);
  }
  const t = el('div', { class: `toast ${kind}` }, message);
  wrap.append(t);
  setTimeout(() => t.remove(), 5000);
}

export function onAuthInvalid(cb) {
  window.addEventListener('cairn:auth-invalid', cb);
}

export function absUrl(path) {
  return API_BASE + path;
}

// 复制文本（回退 execCommand）
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.append(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      return true;
    } catch { return false; }
  }
}

export function taskTypeLabel(t) {
  const map = {
    bootstrap: 'bootstrap', reason: 'reason', explore: 'explore',
    verify: 'verify', audit: 'audit', replay: 'replay',
  };
  return map[t] || t || '?';
}

// 状态 → 图标 + 文本（颜色不唯一承载语义，配图标）
export function statusMeta(status) {
  const map = {
    queued: { icon: '○', text: '排队' },
    running: { icon: '●', text: '运行中' },
    success: { icon: '✓', text: '成功' },
    failed: { icon: '✗', text: '失败' },
    cancelled: { icon: '⊘', text: '已取消' },
    unhealthy: { icon: '⚠', text: '异常' },
    rejected: { icon: '⊘', text: '已拒绝' },
  };
  return map[status] || { icon: '?', text: status || '未知' };
}

export function eventKindMeta(kind) {
  const map = {
    step: { icon: '⊹', cls: 'k-step', label: 'step' },
    tool: { icon: '▶', cls: 'k-tool', label: 'tool' },
    command: { icon: '$', cls: 'k-command', label: 'cmd' },
    output: { icon: '⡿', cls: 'k-output', label: 'out' },
    status: { icon: '⚑', cls: 'k-status', label: 'status' },
    error: { icon: '⚠', cls: 'k-error', label: 'error' },
  };
  return map[kind] || { icon: '·', cls: 'k-output', label: kind || '?' };
}

// 从 outcome_note（30 写入的 JSON 元数据）解析业务标签：
// {"finding_id":"fd-001"} / {"coverage_item_ids":["c-013"]} / {"retest_round":2} / {"phase":"..."}
export function parseOutcomeNote(note) {
  if (!note) return {};
  const s = String(note).trim();
  if (!s || s[0] !== '{') return { text: s };
  try {
    const obj = JSON.parse(s);
    if (obj && typeof obj === 'object') return obj;
  } catch { /* 非 JSON 摘要，作为文本展示 */ }
  return { text: s };
}

export function deepLinkFinding(eid, fid) {
  return `#/engagements/${eid}/findings/${fid}`;
}
