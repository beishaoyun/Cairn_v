// heatmap-view.js — 覆盖热力图（coverage spec §4）
//  目标×测试项矩阵；cell 状态色 + 部分覆盖半色（C9）；500ms 无交互自适应列宽
//  cell 点击 → 抽屉详情 + 人工动作引导（豁免/标记不适用/调整深度/强制校准，服务端仍 gate）
//  过滤：状态 / category / 优先级阈值；复测轮次徽标；audit_discrepancy ⚠；5s 轮询
import { api } from '../api.js';
import { el, toast, fmtClock, statusMeta } from '../ui.js';
import { bus } from '../store.js';

const PRIO_THRESHOLD = 0.3;

export class HeatmapView {
  constructor(eid) {
    this.eid = eid;
    this.data = { targets: [], test_types: [], cells: [], summary: {} };
    this.filters = { status: '', category: '', threshold: 0 };
    this.drawer = null;
    this.timer = null;
    this.root = null;
    this._destroyed = false;
    this._resizeTimer = null;
  }

  render() {
    this.root = el('div', { class: 'heatmap-wrap' });
    this.root.innerHTML = '<div class="loading"><span class="spin"></span> 加载热力图…</div>';
    return this.root;
  }

  async mount() {
    await this.load();
    this.timer = setInterval(() => this.load(), 5000);
    window.addEventListener('resize', this._onResizeDebounced);
  }

  unmount() {
    this._destroyed = true;
    clearInterval(this.timer);
    window.removeEventListener('resize', this._onResizeDebounced);
    this.closeDrawer();
  }

  _onResizeDebounced = () => {
    clearTimeout(this._resizeTimer);
    this._resizeTimer = setTimeout(() => this._adaptColumns(), 500);
  };

  async load() {
    try {
      const data = await api.get(`/engagements/${this.eid}/coverage`);
      this.data = data;
      if (!this._destroyed) this._render();
    } catch (e) {
      if (!this._destroyed) {
        this.root.replaceChildren(el('div', { class: 'alert-banner', text: `热力图加载失败：${e.message}` }));
      }
    }
  }

  // ---------- 渲染 ----------
  _render() {
    const { targets, test_types, cells, summary } = this.data;
    const cellByKey = new Map(cells.map((c) => [`${c.target_id}:${c.test_type_id}`, c]));

    // category 分组表头
    const cats = [];
    for (const tt of test_types) {
      if (!cats.find((c) => c.id === tt.category)) cats.push({ id: tt.category, name: catLabel(tt.category) });
    }

    const table = el('table', { class: 'heat-table' });
    const thead = el('thead');
    table.append(thead);
    thead.append(...this._headerRows(test_types, cats));

    const tbody = el('tbody');
    for (const t of targets) {
      const tr = el('tr');
      tr.append(el('td', { class: 'target' },
        el('span', { title: `criticality ${t.criticality}` }, t.value),
        el('span', { class: 'prio', text: ` c${t.criticality}` }),
      ));
      for (const tt of test_types) {
        const cell = cellByKey.get(`${t.id}:${tt.id}`);
        tr.append(this._cell(t, tt, cell));
      }
      tbody.append(tr);
    }
    table.append(tbody);

    // 图例
    const legend = el('div', { class: 'heat-legend' },
      lg('c-untested-hi', '未测·高优先'), lg('c-untested-lo', '未测·低优先'),
      lg('c-in-progress', '测试中'), lg('c-tested', '无问题'),
      lg('c-tested-partial', '部分覆盖⚠'), lg('c-finding', '有发现'), lg('c-na', '不适用/豁免'),
    );

    // 过滤条：状态 / 优先级阈值滑块
    const statusSel = el('select', {
      onchange: (e) => { this.filters.status = e.target.value; this._render(); },
    },
      el('option', { value: '', text: '全部状态' }),
      ...['untested', 'in_progress', 'tested_no_issue', 'tested_with_finding', 'not_applicable', 'waived']
        .map((s) => el('option', { value: s, text: s })),
    );
    const threshold = el('input', {
      type: 'range', min: 0, max: 1, step: 0.05, value: this.filters.threshold,
      oninput: (e) => { this.filters.threshold = Number(e.target.value); this._render(); },
    });
    const filterBar = el('div', { class: 'timeline-filters' },
      statusSel,
      el('span', { class: 'muted', text: '高优先阈值' }), threshold, el('span', { class: 'muted', text: this.filters.threshold || PRIO_THRESHOLD }),
    );

    this.root.replaceChildren(filterBar, legend, el('div', { class: 'heatmap-scroll' }, table));
    this._adaptColumns();
  }

  _headerRows(test_types, cats) {
    const rows = [];
    // 分类跨列行
    const catRow = el('tr');
    catRow.append(el('th', { class: 'cat', rowspan: 2, text: '目标 \\ 测试项' }));
    for (const c of cats) {
      const span = test_types.filter((tt) => tt.category === c.id).length;
      catRow.append(el('th', { class: 'cat', colspan: span || 1, text: c.name }));
    }
    rows.push(catRow);
    // 测试项行
    const ttRow = el('tr');
    for (const tt of test_types) {
      ttRow.append(el('th', { class: 'tt', title: `${tt.name} (risk ${tt.risk})`, text: tt.name }));
    }
    rows.push(ttRow);
    return rows;
  }

  _cell(target, tt, cell) {
    if (!cell) return el('td', { class: 'cell c-untested-lo', title: '无覆盖项' }, '·');
    const cls = this._cellClass(cell);
    // C9：部分覆盖格 ✓+⚠ 角标；audit_discrepancy 也显示 ⚠（coverage spec §5.9）
    const partialMark = cell.status === 'tested_no_issue' && cell.partial ? '⚠' : '';
    const auditMark = cell.last_result === 'audit_discrepancy' ? '⚠' : '';
    const mark = (cell.status === 'tested_no_issue' ? '✓' : cell.status === 'tested_with_finding' ? '●' : (cell.status === 'not_applicable' || cell.status === 'waived') ? 'ⓘ' : '') + partialMark + auditMark;
    const prioNum = cell.status === 'untested' && cell.priority >= (this.filters.threshold || PRIO_THRESHOLD) ? cell.priority : '';
    const retest = cell.retest_round > 0 ? `⤾${cell.retest_round}` : '';
    const audit = cell.last_result === 'audit_discrepancy' ? '⚠' : '';
    const dimmed = this.filters.status && cell.status !== this.filters.status;
    const td = el('td', {
      class: `cell ${cls} ${dimmed ? 'dimmed' : ''}`,
      style: dimmed ? 'opacity:.25' : '',
      title: this._cellTitle(cell),
      onclick: () => this.openDrawer(target, tt, cell),
    },
      el('span', { class: 'mark' }, mark + audit),
      prioNum ? el('span', { class: 'prio', text: prioNum }) : null,
      retest ? el('span', { class: 'retest', text: retest }) : null,
    );
    return td;
  }

  _cellClass(cell) {
    const s = cell.status;
    if (s === 'in_progress') return 'c-in-progress';
    if (s === 'tested_no_issue') return cell.partial ? 'c-tested-partial' : 'c-tested';
    if (s === 'tested_with_finding') return 'c-finding';
    if (s === 'not_applicable') return 'c-na';
    if (s === 'waived') return 'c-waived';
    // untested
    const hi = cell.priority >= (this.filters.threshold || PRIO_THRESHOLD);
    return hi ? 'c-untested-hi' : 'c-untested-lo';
  }

  _cellTitle(cell) {
    const depth = { baseline: '基础', standard: '标准', deep: '深度' }[cell.depth_required] || cell.depth_required;
    return [
      `${cell.item_id} · ${cell.status} · depth ${depth}`,
      `priority ${cell.priority} · partial ${cell.partial ? '是' : '否'} · retest_round ${cell.retest_round}`,
      cell.last_result ? `last_result: ${cell.last_result}` : '',
      cell.tested_at ? `tested_at: ${fmtClock(cell.tested_at)}` : '',
    ].join('\n');
  }

  _adaptColumns() {
    const wrap = this.root.querySelector('.heatmap-scroll');
    if (!wrap) return;
    const cols = this.data.test_types.length || 1;
    const avail = wrap.clientWidth || 800;
    const w = Math.max(40, Math.floor(avail / cols));
    wrap.querySelectorAll('.cell').forEach((td) => { td.style.minWidth = `${Math.min(90, w)}px`; });
  }

  // ---------- 抽屉：详情 + 人工动作引导 ----------
  openDrawer(target, tt, cell) {
    this.closeDrawer();
    const drawer = el('div', { class: 'drawer' });
    const depthOptions = ['baseline', 'standard', 'deep'].map((d) => el('option', { value: d, text: d }));
    const findLink = cell.status === 'tested_with_finding'
      ? el('p', {}, '关联 finding：', el('a', { href: `#/engagements/${this.eid}/findings`, text: '前往 Findings 面板 →' }))
      : null;

    drawer.append(
      el('div', { class: 'drawer-head', style: 'display:flex;justify-content:space-between' },
        el('h3', { text: cell.item_id }),
        el('button', { class: 'btn ghost small', onclick: () => this.closeDrawer() }, '×'),
      ),
      el('div', { class: 'field' }, el('label', { text: '目标' }), el('div', { text: `${target.value} (criticality ${target.criticality})` })),
      el('div', { class: 'field' }, el('label', { text: '测试项' }), el('div', { text: `${tt.name} (${catLabel(tt.category)})` })),
      el('div', { class: 'field' }, el('label', { text: '状态' }), el('div', { text: statusMeta(cell.status).text })),
      el('div', { class: 'field' }, el('label', { text: '深度' }), el('div', { text: cell.depth_required })),
      el('div', { class: 'field' }, el('label', { text: '优先级' }), el('div', { text: cell.priority })),
      el('div', { class: 'field' }, el('label', { text: '部分覆盖' }), el('div', { text: cell.partial ? '是（C9 半色）' : '否' })),
      el('div', { class: 'field' }, el('label', { text: '复测轮次' }), el('div', { text: cell.retest_round || 0 })),
      cell.last_result ? el('div', { class: 'field' }, el('label', { text: '最近结果' }), el('div', { text: cell.last_result })) : null,
      findLink,
      el('hr', {}),
      el('p', { class: 'muted', text: '人工操作（H）——仅引导，服务端强制 gate（Agent 不持 token）。' }),

      // 豁免 / 标记不适用
      el('div', { class: 'field' },
        el('label', { text: '豁免 / 标记不适用' }),
        el('select', { id: 'waive-kind' },
          el('option', { value: 'not_applicable', text: 'not_applicable（标记不适用）' }),
          el('option', { value: 'out_of_scope', text: 'out_of_scope' }),
          el('option', { value: 'risk_accepted', text: 'risk_accepted' }),
        ),
      ),
      el('div', { class: 'field' }, el('label', { text: '理由（必填）' }), el('input', { id: 'waive-reason', placeholder: '为什么豁免？' })),
      el('button', {
        class: 'btn small', onclick: () => this.waive(cell.item_id),
      }, '提交豁免'),
      el('hr', {}),

      // 调整深度
      el('div', { class: 'field' }, el('label', { text: '调整深度' }), el('select', { id: 'depth-select' }, ...depthOptions)),
      el('button', {
        class: 'btn small ghost', onclick: () => this.setDepth(cell.item_id),
      }, '调整深度'),
      el('hr', {}),

      // 强制校准
      el('div', { class: 'field' },
        el('label', { text: '强制校准状态' }),
        el('select', { id: 'calib-status' },
          el('option', { value: 'untested', text: 'untested' }),
          el('option', { value: 'tested_no_issue', text: 'tested_no_issue' }),
          el('option', { value: 'tested_with_finding', text: 'tested_with_finding' }),
          el('option', { value: 'in_progress', text: 'in_progress' }),
        ),
      ),
      el('button', {
        class: 'btn small ghost', onclick: () => this.calibrate(cell.item_id),
      }, '强制校准'),
    );

    this.drawer = drawer;
    document.body.append(el('div', { class: 'drawer-backdrop', onclick: () => this.closeDrawer() }), drawer);
  }

  closeDrawer() {
    if (this.drawer) { this.drawer.remove(); this.drawer = null; }
    const backdrop = document.querySelector('.drawer-backdrop');
    if (backdrop) backdrop.remove();
  }

  async waive(itemId) {
    const kind = document.getElementById('waive-kind')?.value;
    const reason = document.getElementById('waive-reason')?.value.trim();
    if (!reason) return toast('豁免必须填写理由（B4）', 'error');
    try {
      await api.post(`/engagements/${this.eid}/coverage/items/${itemId}/waive`, { kind, reason, by: 'human' });
      toast('豁免已提交', 'success');
      this.load();
    } catch (e) {
      toast(`豁免失败：${e.message}`, 'error');
    }
  }

  async setDepth(itemId) {
    const depth = document.getElementById('depth-select')?.value;
    try {
      await api.put(`/engagements/${this.eid}/coverage/items/${itemId}`, { depth_required: depth });
      toast('深度已调整', 'success');
      this.load();
    } catch (e) {
      toast(`调整深度失败：${e.message}`, 'error');
    }
  }

  async calibrate(itemId) {
    const status = document.getElementById('calib-status')?.value;
    try {
      await api.put(`/engagements/${this.eid}/coverage/items/${itemId}`, { status });
      toast('校准已提交', 'success');
      this.load();
    } catch (e) {
      toast(`校准失败：${e.message}`, 'error');
    }
  }
}

function catLabel(cat) {
  const map = { recon: '侦察', scan: '扫描', webapp: 'Web应用', network: '网络', config: '配置', osint: 'OSINT', auth: '认证', other: '其他' };
  return map[cat] || cat;
}

function lg(cls, text) {
  return el('span', { class: 'lg' }, el('span', { class: `swatch ${cls}` }), text);
}
