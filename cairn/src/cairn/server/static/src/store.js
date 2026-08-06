// store.js — 极简响应式 store + 事件总线（轻量栈，替代框架状态管理）
// 视图组件订阅自己关心的 slice，变化时重新渲染对应 DOM 片段。

export function createStore(initial = {}) {
  const state = { ...initial };
  const listeners = new Set();
  return {
    getState: () => state,
    setState(patch) {
      Object.assign(state, patch);
      listeners.forEach((fn) => fn(state));
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

// 全局 store：跨视图共享（token 由 localStorage 托管，不落 store）
export const store = createStore({
  route: { name: 'engagements', params: {} },
  engagement: null,      // 当前 Engagement
  tasks: [],             // 活动面板任务列表
  activeTasks: [],       // active=true 汇总轮询
  heatmap: null,         // coverage 矩阵
  findings: [],          // findings 列表
  findingsPending: [],   // pending_verify 清单
  timeline: [],          // 时间线列表
  projects: [],          // 图项目列表
  report: null,          // 最新报告
  stats: null,           // engagement stats
  lastError: null,
});

// 轻量事件总线（跨视图联动：explore 成功→热力图刷新、verify running→finding 脉冲…）
const busListeners = new Map();
export const bus = {
  on(evt, fn) {
    if (!busListeners.has(evt)) busListeners.set(evt, new Set());
    busListeners.get(evt).add(fn);
    return () => busListeners.get(evt)?.delete(fn);
  },
  emit(evt, data) {
    const set = busListeners.get(evt);
    if (set) set.forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
  },
};
