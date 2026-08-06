// api.js — Cairn Server REST 客户端（Bearer token，统一错误规范化）
// 契约：skeleton §2 / progress §4 / coverage §4.1；错误体 {"error_code","message","detail"}
//
// 注意：SSE 一律走一次性 ticket（POST /tasks/{id}/events/ticket），绝不把 Bearer token 放 URL；
// 本模块只负责 JSON 请求（fetch 可带 Header），SSE 见 sse.js。

const TOKEN_KEY = 'cairn_token';
// API 基址：默认同源（由 Cairn Server 静态托管）；Vite 开发代理或独立部署可覆盖。
export const API_BASE = (window.CAIRN_API_BASE || '').replace(/\/$/, '');

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}
export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t || '');
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, error_code, message, detail) {
    super(message);
    this.status = status;
    this.error_code = error_code;
    this.detail = detail;
  }
}

async function request(method, path, body, { raw = false } = {}) {
  const url = API_BASE + path;
  const headers = { Accept: 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload = undefined;
  if (body !== undefined && body !== null) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, { method, headers, body: payload });
  } catch (e) {
    throw new ApiError(0, 'NETWORK', `网络错误：${e.message}`);
  }
  if (resp.status === 401) {
    // 令牌失效 → 通知全局切回令牌页
    window.dispatchEvent(new CustomEvent('cairn:auth-invalid'));
  }
  if (resp.status === 204) return raw ? null : null;
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!resp.ok) {
    if (data && typeof data === 'object' && data.error_code) {
      throw new ApiError(resp.status, data.error_code, data.message || text, data.detail);
    }
    throw new ApiError(resp.status, 'HTTP_' + resp.status, text || resp.statusText, null);
  }
  return raw ? text : data;
}

export const api = {
  get: (path, opts) => request('GET', path, undefined, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
  put: (path, body, opts) => request('PUT', path, body, opts),
  del: (path, opts) => request('DELETE', path, undefined, opts),
};
