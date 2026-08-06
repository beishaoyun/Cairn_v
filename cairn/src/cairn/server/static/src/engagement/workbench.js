// workbench.js — Engagement 工作台（frontend §1.2 布局）
//  ① 顶部进度条带（覆盖率 / 进行中 / 排队 / 待复核 / 复测中 / 异常 / finalize 提示）
//  ② 任务活动面板（默认展开）  ③ 业务 Tab：覆盖热力图 / Findings / 时间线 / 图工作区 / 报告
import { api } from '../api.js';
import { el, toast, taskTypeLabel, statusMeta, fmtDuration, parseOutcomeNote, relativeTime } from '../ui.js';
import { store, bus } from '../store.js';
import { ActivityPanel } from '../activity/activity-panel.js';
import { HeatmapView } from '../heatmap/heatmap-view.js';
import { FindingsView } from '../findings/findings-view.js';
import { TimelineView } from '../timeline/timeline-view.js';
import { GraphView } from '../graph/graph-view.js';
import { ReportView } from '../report/report-view.js';

const TABS = [
  { id: 'activity', label: '任务活动', cls: 'tab' },
  { id: 'heatmap', label: '覆盖热力图', cls: 'tab' },
  { id: 'findings', label: 'Findings', cls: 'tab' },
  { id: 'timeline', label: '时间线', cls: 'tab' },
  { id: 'graph', label: '图工作区', cls: 'tab' },
  { id: 'report', label: '报告', cls: 'tab' },
];

export async function renderWorkbench(eid) {
  let engagement = null;
  try {
    engagement = await api.get(`/engagements/${eid}`);
  } catch (e) {
    return el('div', { class: 'empty' }, `加载 Engagement 失败：${e.message}`);
  }
  store.setState({ engagement });

  const state = { tab: 'activity', busy: false };
  const tabBar = el('div', { class: 'tabbar' });
  const tabBody = el('div', { class: 'tab-body' });
  let stripEl = null;
  let activityPanel = null;
  let currentView = null;
  let stripTimer = null;

  // ---------- 顶部进度条带 ----------
  function renderStrip() {
    const s = store.getState();
    const heat = s.heatmap || {};
    const tasks = s.tasks || [];
    const findings = s.findings || [];
    const summary = heat.summary || {};
    const running = tasks.filter((t) => t.status === 'running').length;
    const queued = tasks.filter((t) => t.status === 'queued').length;
    const pendingVerify = findings.filter((f) => f.status === 'pending_verify' || f.verify_status === 'pending').length;
    const retesting = tasks.filter((t) => t.task_type === 'replay' && (t.status === 'queued' || t.status === 'running')).length;
    const retestVerify = tasks.filter((t) => t.task_type === 'verify' && t.status === 'running' && String(t.task_type) === 'verify').length;
    const errored = tasks.filter((t) => ['failed', 'rejected', 'unhealthy'].includes(t.status)).length;
    const ratio = summary.coverage_ratio != null ? Math.round(summary.coverage_ratio * 100) : null;
    const total = summary.total || 0;

    const items = [];
    if (ratio !== null) {
      items.push(
        el('span', { class: 'strip-item' },
          el('span', { text: '覆盖率' }),
          el('div', { class: 'progress-outer' },
            el('div', { class: 'progress-inner', style: `width:${Math.min(100, ratio)}%` }),
          ),
          el('b', { text: `${ratio}%` }),
        ),
      );
    }
    items.push(el('span', { class: 'strip-item' }, el('span', { class: 'dot', style: 'background:var(--blue)' }), el('span', { text: '进行中' }), el('b', { text: running })));
    items.push(el('span', { class: 'strip-item' }, el('span', { class: 'dot', style: 'background:var(--gray)' }), el('span', { text: '排队' }), el('b', { text: queued })));
    items.push(el('span', { class: 'strip-item' }, el('span', { class: 'dot', style: 'background:var(--orange)' }), el('span', { text: '待复核' }), el('b', { text: pendingVerify })));
    items.push(el('span', { class: 'strip-item' }, el('span', { class: 'dot', style: 'background:var(--amber)' }), el('span', { text: '复测中' }), el('b', { text: retesting })));
    items.push(el('span', { class: 'strip-item' }, el('span', { text: '覆盖项' }), el('b', { text: `${summary.covered || 0}/${total}` })));

    // failed/rejected → 顶部告警
    if (errored > 0) {
      items.push(el('span', {
        class: 'alert-banner',
        onclick: () => { state.tab = 'activity'; activate('activity'); },
      }, `${errored} 个任务异常，点击查看`));
    }
    // 全部任务完成且覆盖满足 → 「可 finalize」提示条
    const canFinalize = checkLikelyReady(summary, tasks);
    if (canFinalize) {
      items.push(el('span', {
        class: 'finalize-banner',
        onclick: () => { state.tab = 'report'; activate('report'); },
      }, '覆盖策略已满足，可提交人工收尾 finalize →'));
    }
    return el('div', { class: 'top-strip' }, items);
  }

  function checkLikelyReady(summary, tasks) {
    if (!summary || summary.total === 0) return false;
    const ratioOk = summary.coverage_ratio != null && summary.coverage_ratio >= 0.95;
    const noHighGaps = (summary.untested || 0) === 0;
    const noRunning = !tasks.some((t) => t.status === 'running' || t.status === 'queued');
    return ratioOk && noHighGaps && noRunning;
  }

  // ---------- Tab ----------
  async function activate(tabId) {
    // 切 Tab 前卸载旧视图（清定时器/SSE，防泄漏）
    currentView?.unmount?.();
    currentView = null;
    state.tab = tabId;
    tabBar.replaceChildren(...TABS.map((t) =>
      el('div', { class: `tab ${t.cls} ${t.id === tabId ? 'active' : ''}`, onclick: () => activate(t.id) }, t.label),
    ));
    tabBody.replaceChildren(el('div', { class: 'loading' }, el('span', { class: 'spin' }), ' 加载中…'));
    const views = {
      activity: renderActivity,
      heatmap: renderHeatmap,
      findings: renderFindings,
      timeline: renderTimeline,
      graph: renderGraph,
      report: renderReport,
    };
    currentView = await views[tabId](tabBody);
  }

  async function renderActivity(body) {
    if (!activityPanel) activityPanel = new ActivityPanel(eid);
    const node = activityPanel.render();
    body.replaceChildren(node);
    await activityPanel.mount();
    return { unmount: () => activityPanel.unmount() };
  }

  async function renderHeatmap(body) {
    const v = new HeatmapView(eid);
    const node = v.render();
    body.replaceChildren(node);
    v.mount();
    return { unmount: () => v.unmount() };
  }
  async function renderFindings(body) {
    const v = new FindingsView(eid);
    const node = v.render();
    body.replaceChildren(node);
    v.mount();
    return { unmount: () => v.unmount() };
  }
  async function renderTimeline(body) {
    const v = new TimelineView(eid);
    const node = v.render();
    body.replaceChildren(node);
    v.mount();
    return { unmount: () => v.unmount() };
  }
  async function renderGraph(body) {
    const v = new GraphView(eid);
    const node = v.render();
    body.replaceChildren(node);
    v.mount();
    return { unmount: () => v.unmount() };
  }
  async function renderReport(body) {
    const v = new ReportView(eid);
    const node = v.render();
    body.replaceChildren(node);
    v.mount();
    return { unmount: () => v.unmount() };
  }

  // ---------- 数据加载（跨视图共享 + 联动） ----------
  async function refreshTasks() {
    try {
      const tasks = await api.get(`/engagements/${eid}/tasks?limit=100`);
      store.setState({ tasks });
      stripEl?.replaceChildren(...renderStrip().children);
    } catch (e) { /* 静默（网络抖动） */ }
  }

  async function refreshHeatmap() {
    try {
      const heat = await api.get(`/engagements/${eid}/coverage`);
      store.setState({ heatmap: heat });
      stripEl?.replaceChildren(...renderStrip().children);
      bus.emit('heatmap:update', heat);
    } catch (e) { /* 静默 */ }
  }

  async function refreshFindings() {
    try {
      const data = await api.get(`/engagements/${eid}/findings?limit=200`);
      store.setState({ findings: data.items || [] });
      stripEl?.replaceChildren(...renderStrip().children);
      bus.emit('findings:update', data.items || []);
    } catch (e) { /* 静默 */ }
  }

  // 业务联动：explore success 写回 → 热力图刷新；verify 终态 → findings 刷新
  const offTerminal = bus.on('activity:task-terminal', (task) => {
    if (task.task_type === 'explore') refreshHeatmap();
    if (['verify', 'replay'].includes(task.task_type)) refreshFindings();
  });
  // 时间线 / Findings 跳转 → 切换 Tab
  const offTab = bus.on('workbench:switch-tab', (tabId) => activate(tabId));

  // 周期轮询（5s 汇总；连接数控制见 activity-panel）
  stripTimer = setInterval(() => { refreshTasks(); }, 5000);

  await Promise.all([refreshTasks(), refreshHeatmap(), refreshFindings()]);
  stripEl = renderStrip();

  const shell = el('div', { class: 'workbench' }, stripEl, tabBar, tabBody);
  activate('activity'); // 默认展开活动面板

  return {
    node: shell,
    cleanup() {
      clearInterval(stripTimer);
      offTerminal();
      offTab();
      currentView?.unmount?.();
    },
  };
}
