// findings-view.js — 漏洞面板（frontend §5 业务联动 / capture spec §5 状态机）
//  pending_verify 清单 + 全量；verify running → 徽标脉冲「复核中」
//  confirmed → severity 双轨（agent→verified，如 high→critical）；rejected → 「待人工确认」
//  retest ⤾ 轮次徽标；点击展开详情（描述/证据/历史/命令/http）
import { api } from '../api.js';
import { el, toast, fmtClock, statusMeta } from '../ui.js';
import { store } from '../store.js';

const STATUS_LABEL = {
  open: '开放', pending_verify: '待复核', pending_false_positive: '待人工确认',
  verified: '已复核', needs_review: '需人工介入', fixed: '已修复',
  false_positive: '误报', accepted: '已接受', closed: '已关闭',
};

export class FindingsView {
  constructor(eid) {
    this.eid = eid;
    this.findings = [];
    this.expanded = new Set();
    this.filter = { status: '', q: '' };
    this.timer = null;
    this.root = null;
    this._destroyed = false;
  }

  render() {
    this.root = el('div', { class: 'findings-view' });
    this.root.innerHTML = '<div class="loading"><span class="spin"></span> 加载 Findings…</div>';
    return this.root;
  }

  async mount() {
    await this.load();
    this.timer = setInterval(() => this.load(), 5000);
  }

  unmount() {
    this._destroyed = true;
    clearInterval(this.timer);
  }

  async load() {
    try {
      const data = await api.get(`/engagements/${this.eid}/findings?limit=200`);
      this.findings = data.items || [];
      if (!this._destroyed) this._render();
    } catch (e) {
      if (!this._destroyed) {
        this.root.replaceChildren(el('div', { class: 'alert-banner', text: `Findings 加载失败：${e.message}` }));
      }
    }
  }

  // 运行中的 verify 任务（从活动面板 store 读取）
  _runningVerifyFindingIds() {
    const tasks = store.getState().tasks || [];
    const ids = new Set();
    let anyRunning = false;
    for (const t of tasks) {
      if (t.task_type === 'verify' && t.status === 'running') {
        anyRunning = true;
        try {
          const note = JSON.parse(t.outcome_note || '');
          if (note && note.finding_id) ids.add(note.finding_id);
        } catch { /* noop */ }
      }
    }
    return { ids, anyRunning };
  }

  _render() {
    const { ids, anyRunning } = this._runningVerifyFindingIds();
    const filtered = this.findings.filter((f) => {
      if (this.filter.status && f.status !== this.filter.status) return false;
      if (this.filter.q) {
        const q = this.filter.q.toLowerCase();
        if (!(f.title || '').toLowerCase().includes(q) && !(f.id || '').toLowerCase().includes(q)) return false;
      }
      return true;
    });

    const statusFilter = el('select', {
      onchange: (e) => { this.filter.status = e.target.value; this._render(); },
    },
      el('option', { value: '', text: '全部状态' }),
      ...Object.entries(STATUS_LABEL).map(([k, v]) => el('option', { value: k, text: v })),
    );
    const qInput = el('input', {
      placeholder: '搜索标题 / id…', style: 'padding:5px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-panel-2);color:var(--text)',
      oninput: (e) => { this.filter.q = e.target.value; this._render(); },
    });

    const list = el('div');
    if (!filtered.length) {
      list.append(el('div', { class: 'empty', text: '暂无 Findings' }));
    }
    for (const f of filtered) {
      list.append(this._card(f, ids, anyRunning));
    }

    this.root.replaceChildren(
      el('div', { class: 'timeline-filters' }, statusFilter, qInput, el('span', { class: 'muted', text: `${filtered.length}/${this.findings.length}` })),
      list,
    );
  }

  _card(f, runningIds, anyRunning) {
    const isExpanded = this.expanded.has(f.id);
    const effective = f.verified_severity || f.severity || f.agent_severity;
    const verifying = runningIds.has(f.id) || (anyRunning && f.status === 'pending_verify');
    const dual = f.verified_severity && f.agent_severity && f.verified_severity !== f.agent_severity
      ? el('span', { class: 'dual-track', text: `${f.agent_severity} → ${f.verified_severity}` })
      : null;
    const retest = f.retest_round > 0 ? el('span', { class: 'biz-tag', text: `⤾${f.retest_round} 复测` }) : null;
    const retestPass = f.retest_pass > 0 ? el('span', { class: 'biz-tag', text: `确认×${f.retest_pass}` }) : null;

    const head = el('div', { class: 'finding-head' },
      el('span', { class: `sev-badge sev-${effective}` }, (effective || '?').toUpperCase()),
      el('span', { class: 'status-badge', style: `background:${statusColor(f.status)}22;color:${statusColor(f.status)}` }, STATUS_LABEL[f.status] || f.status),
      verifying ? el('span', { class: 'status-badge st-running', text: '复核中' }) : null,
      el('strong', { text: f.title || f.id }),
      dual,
      el('span', { class: 'muted', style: 'font-size:11px' },
        f.cvss_score != null ? `CVSS ${f.cvss_score} · ` : '',
        f.cwe_id ? `${f.cwe_id} · ` : '',
        f.category ? f.category : '',
      ),
      retest, retestPass,
      el('span', { class: 'spacer', style: 'flex:1' }),
      el('span', { class: 'act-expand', text: isExpanded ? '▾' : '▸' }),
    );

    const card = el('div', {
      class: `finding-card ${verifying ? 'verify-pulse' : ''}`,
      onclick: () => { this.expanded.has(f.id) ? this.expanded.delete(f.id) : this.expanded.add(f.id); this._render(); },
    }, head);

    if (isExpanded) {
      const body = el('div', { class: 'finding-body' },
        f.description ? el('div', { class: 'finding-desc', text: f.description }) : null,
        el('div', { class: 'finding-meta' },
          `id ${f.id} · ${f.asset || '—'} · detected_by ${f.detected_by || '—'} · created ${fmtClock(f.created_at)}` +
          (f.verify_status && f.verify_status !== 'none' ? ` · verify:${f.verify_status}` : '') +
          (f.reverify_count ? ` · reverify×${f.reverify_count}` : ''),
        ),
        f.remediation ? el('div', { class: 'finding-desc', text: `修复建议：${f.remediation}` }) : null,
        el('div', { class: 'finding-meta' },
          '状态流转历史见 GET /engagements/{id}/findings/' + f.id + '/history（只读，本面板聚焦实时联动）',
        ),
      );
      card.append(body);
    }
    return card;
  }
}

function statusColor(status) {
  const map = {
    open: 'var(--blue)', pending_verify: 'var(--orange)', pending_false_positive: 'var(--amber)',
    verified: 'var(--green)', needs_review: 'var(--red)', fixed: 'var(--purple)',
    false_positive: 'var(--gray)', accepted: 'var(--gray)', closed: 'var(--gray)',
  };
  return map[status] || 'var(--text-dim)';
}
