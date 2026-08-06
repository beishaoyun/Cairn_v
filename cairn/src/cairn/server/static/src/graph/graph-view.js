// graph-view.js — 图工作区（exploration-graph-spec §5 / 25 交接物；只读展示）
//  渲染 facts/intents/hints：origin/goal 特殊节点标注；intent 超边 from→to
//  open intent 标注认领 worker（虚线） / concluded 置灰；点击 intent → 时间线
//  导出 YAML 按钮（GET /projects/{pid}/export?format=yaml，fetch 带 Bearer + Blob 下载）
import { api } from '../api.js';
import { el, toast, esc, fmtClock } from '../ui.js';
import { bus } from '../store.js';

export class GraphView {
  constructor(eid) {
    this.eid = eid;
    this.projects = [];
    this.pid = null;
    this.data = null;
    this.root = null;
    this._destroyed = false;
  }

  render() {
    this.root = el('div', { class: 'graph-view' });
    this.root.innerHTML = '<div class="loading"><span class="spin"></span> 加载探索图…</div>';
    return this.root;
  }

  async mount() {
    await this.loadProjects();
  }

  unmount() { this._destroyed = true; }

  async loadProjects() {
    try {
      this.projects = await api.get(`/projects?engagement_id=${this.eid}`);
      if (this.projects.length) {
        this.pid = this.projects[0].id;
        await this.loadProject();
      } else {
        this._render();
      }
    } catch (e) {
      this.root.replaceChildren(el('div', { class: 'alert-banner', text: `项目列表加载失败：${e.message}` }));
    }
  }

  async loadProject() {
    try {
      this.data = await api.get(`/projects/${this.pid}`);
      if (!this._destroyed) this._render();
    } catch (e) {
      this.root.replaceChildren(el('div', { class: 'alert-banner', text: `项目详情加载失败：${e.message}` }));
    }
  }

  _render() {
    const select = el('select', {
      onchange: (e) => { this.pid = e.target.value; this.loadProject(); },
    }, ...this.projects.map((p) => el('option', { value: p.id, selected: p.id === this.pid }, `${p.id} · ${p.title}`)));

    const exportBtn = el('button', {
      class: 'btn small',
      onclick: () => this.exportYaml(),
    }, '导出 YAML');

    const toolbar = el('div', { class: 'graph-toolbar' }, select, exportBtn, el('span', { class: 'muted' }, '只读展示 · 人工操作由服务端 gate'));

    if (!this.data) {
      this.root.replaceChildren(toolbar, el('div', { class: 'empty', text: '暂无探索图项目' }));
      return;
    }
    const facts = this.data.facts || [];
    const intents = this.data.intents || [];
    const hints = this.data.hints || [];

    const svgWrap = this._buildGraph(facts, intents);
    const legend = el('div', { class: 'graph-legend' },
      el('span', { style: 'color:var(--green)' }, '■ origin（根）'),
      el('span', { style: 'color:var(--red)' }, '■ goal（目标陈述，非完成终态）'),
      el('span', { style: 'color:var(--blue)' }, '-- open intent（已认领）'),
      el('span', { style: 'color:var(--gray)' }, '— concluded intent'),
    );
    const hintList = el('div', { class: 'graph-hints' },
      el('h4', { text: `Hints（${hints.length}）` }),
      hints.length ? hints.map((h) => el('span', { class: 'hint-chip', title: `by ${h.creator} @ ${fmtClock(h.created_at)}` }, `💡 ${h.content}`)) : el('span', { class: 'muted', text: '无' }),
    );

    const info = el('div', { class: 'muted', style: 'margin-top:8px' },
      `项目 ${this.data.id} · status ${this.data.status} · 事实 ${facts.length} · intent ${intents.length} · created ${fmtClock(this.data.created_at)}`,
    );

    this.root.replaceChildren(toolbar, svgWrap, legend, hintList, info);
  }

  // ---------- SVG 渲染 ----------
  _buildGraph(facts, intents) {
    const W = 900, H = 460, cx = W / 2, cy = H / 2, R = Math.min(cx, cy) - 60;
    const special = { origin: null, goal: null };
    const pos = new Map();
    let i = 0;
    for (const f of facts) {
      const desc = (f.description || '').toLowerCase();
      if (desc === 'origin') special.origin = f.id;
      if (desc === 'goal') special.goal = f.id;
      if (desc === 'origin' || desc === 'goal') { pos.set(f.id, { x: cx, y: cy }); continue; }
      const angle = (2 * Math.PI * i) / Math.max(1, facts.length);
      pos.set(f.id, { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) });
      i++;
    }
    // 非特殊节点不占位则补圆
    let k = 0;
    for (const f of facts) {
      if (!pos.has(f.id)) {
        const angle = (2 * Math.PI * k) / Math.max(1, facts.length);
        pos.set(f.id, { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) });
        k++;
      }
    }

    const svg = el('svg', { class: 'graph-svg', viewBox: `0 0 ${W} ${H}`, width: '100%', height: '460' });

    // edges
    for (const it of intents) {
      const open = !it.concluded_at;
      const fromIds = it.from_fact_ids || [];
      const toId = it.to_fact_id;
      const anchor = this._intentAnchor(fromIds, pos);
      for (const fid of fromIds) {
        const p = pos.get(fid);
        if (!p) continue;
        svg.append(el('line', { class: `graph-edge ${open ? 'open' : 'concluded'}`, x1: p.x, y1: p.y, x2: anchor.x, y2: anchor.y }));
      }
      if (toId && pos.get(toId)) {
        const p = pos.get(toId);
        svg.append(el('line', { class: `graph-edge ${open ? 'open' : 'concluded'}`, x1: anchor.x, y1: anchor.y, x2: p.x, y2: p.y }));
      }
    }

    // intent labels
    for (const it of intents) {
      const fromIds = it.from_fact_ids || [];
      const anchor = this._intentAnchor(fromIds, pos);
      const open = !it.concluded_at;
      const label = `${it.id}${it.worker ? ' · ' + it.worker : ''}`;
      const g = el('g', {
        class: 'graph-node intent',
        onclick: () => this._openIntent(it),
        style: 'cursor:pointer',
      },
        el('circle', { cx: anchor.x, cy: anchor.y, r: 6, fill: open ? '#38bdf8' : '#475569', stroke: '#0f172a' }),
        el('text', { x: anchor.x + 9, y: anchor.y + 4, class: 'graph-edge-label', text: label }),
      );
      svg.append(g);
    }

    // fact nodes
    for (const f of facts) {
      const p = pos.get(f.id);
      if (!p) continue;
      const desc = (f.description || '');
      const isOrigin = f.id === special.origin || desc.toLowerCase() === 'origin';
      const isGoal = f.id === special.goal || desc.toLowerCase() === 'goal';
      const label = isOrigin ? 'origin' : isGoal ? 'goal' : (desc.length > 24 ? desc.slice(0, 24) + '…' : desc);
      const boxW = Math.max(70, label.length * 6.2 + 14);
      const boxH = 22;
      const g = el('g', {
        class: `graph-node ${isOrigin ? 'origin' : isGoal ? 'goal' : ''}`,
        transform: `translate(${p.x - boxW / 2}, ${p.y - boxH / 2})`,
      },
        el('rect', { width: boxW, height: boxH, rx: 5 }),
        el('text', { x: boxW / 2, y: boxH / 2 + 4, 'text-anchor': 'middle', text: label }),
      );
      svg.append(g);
    }

    return svg;
  }

  _intentAnchor(fromIds, pos) {
    // 锚点 = from 源的平均位置
    let x = 0, y = 0, n = 0;
    for (const fid of fromIds) {
      const p = pos.get(fid);
      if (p) { x += p.x; y += p.y; n++; }
    }
    if (!n) return { x: 450, y: 230 };
    return { x: x / n, y: y / n };
  }

  _openIntent(it) {
    toast(`intent ${it.id}：${it.description || ''}`, 'info');
    bus.emit('workbench:switch-tab', 'timeline');
  }

  async exportYaml() {
    if (!this.pid) return toast('请先选择项目', 'error');
    try {
      const text = await api.get(`/projects/${this.pid}/export?format=yaml`, { raw: true });
      const blob = new Blob([text], { type: 'application/yaml' });
      const url = URL.createObjectURL(blob);
      const a = el('a', { href: url, download: `${this.pid}.yaml` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('图快照 YAML 已导出', 'success');
    } catch (e) {
      toast(`导出失败：${e.message}`, 'error');
    }
  }
}
