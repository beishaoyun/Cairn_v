// report-view.js — 报告预览 + finalize（41 交接物 §8）
//  GET /engagements/{id}/report 最新报告原文；GET /engagements/{id}/stats 指标
//  POST /engagements/{id}/finalize 人工收尾（409 → 展示 COVERAGE_POLICY_UNMET 明细）
import { api, ApiError } from '../api.js';
import { el, toast, fmtClock } from '../ui.js';

export class ReportView {
  constructor(eid) {
    this.eid = eid;
    this.report = null;
    this.stats = null;
    this.root = null;
    this._destroyed = false;
  }

  render() {
    this.root = el('div', { class: 'report-view' });
    this.root.innerHTML = '<div class="loading"><span class="spin"></span> 加载报告…</div>';
    return this.root;
  }

  async mount() {
    const [report, stats] = await Promise.allSettled([
      api.get(`/engagements/${this.eid}/report`),
      api.get(`/engagements/${this.eid}/stats`),
    ]);
    if (report.status === 'fulfilled') this.report = report.value;
    if (stats.status === 'fulfilled') this.stats = stats.value;
    if (!this._destroyed) this._render();
  }

  unmount() { this._destroyed = true; }

  _render() {
    const finalizeBtn = el('button', {
      class: 'btn',
      onclick: () => this.finalize(),
    }, '提交 finalize（人工收尾）');

    const body = el('div');

    if (this.stats) {
      const cards = el('div', { class: 'stats-grid' });
      const statsItems = this._statsItems();
      for (const [k, v] of statsItems) {
        cards.append(el('div', { class: 'stat-card' }, el('div', { class: 'k', text: k }), el('div', { class: 'v', text: String(v ?? '—') })));
      }
      body.append(el('h3', { text: '指标' }), cards);
    }

    body.append(el('h3', { text: '最新报告' }));
    if (!this.report) {
      body.append(el('div', { class: 'empty', text: '暂无报告（finalize 成功后自动生成 markdown + html；也可手动 POST /engagements/{id}/report）' }));
    } else {
      body.append(el('div', { class: 'muted', style: 'margin-bottom:8px' },
        `${this.report.id} · ${this.report.format} · by ${this.report.generated_by} · ${fmtClock(this.report.created_at)}`));
      const content = String(this.report.content || '');
      if (this.report.format === 'html') {
        const iframe = el('iframe', { style: 'width:100%;height:70vh;border:1px solid var(--border);border-radius:8px;background:#fff', sandbox: 'allow-same-origin' });
        iframe.srcdoc = content;
        body.append(iframe);
      } else {
        body.append(el('pre', { class: 'report-md', text: content }));
      }
    }

    this.root.replaceChildren(
      el('div', { class: 'timeline-filters' }, finalizeBtn, el('span', { class: 'muted', text: 'finalize 仅人工触发；覆盖未达标返回 409 明细，豁免后可重试。' })),
      body,
    );
  }

  _statsItems() {
    const s = this.stats || {};
    const out = [];
    if (s.findings_by_severity) {
      for (const [k, v] of Object.entries(s.findings_by_severity)) out.push([`漏洞 ${k}`, v]);
    }
    if (s.task_success_rate != null) out.push(['任务成功率', `${Math.round(s.task_success_rate * 100)}%`]);
    if (s.total_tasks != null) out.push(['任务总数', s.total_tasks]);
    if (s.coverage_trend && Array.isArray(s.coverage_trend)) out.push(['覆盖趋势记录', s.coverage_trend.length]);
    if (s.verify_runs != null) out.push(['复核记录', s.verify_runs]);
    if (s.replay_runs != null) out.push(['重放记录', s.replay_runs]);
    if (!out.length) {
      // 兜底：平铺非对象字段
      for (const [k, v] of Object.entries(s)) {
        if (v !== null && typeof v !== 'object') out.push([k, v]);
      }
    }
    return out;
  }

  async finalize() {
    try {
      const r = await api.post(`/engagements/${this.eid}/finalize`, {});
      toast(`finalize 成功：${r.message || 'engagement 已完成'}`, 'success');
      // 刷新报告
      const report = await api.get(`/engagements/${this.eid}/report`);
      this.report = report;
      this._render();
    } catch (e) {
      if (e instanceof ApiError && e.error_code === 'COVERAGE_POLICY_UNMET') {
        this._showUnmet(e.detail);
        toast('finalize 未达标：覆盖策略不满足', 'error');
      } else {
        toast(`finalize 失败：${e.message}`, 'error');
      }
    }
  }

  _showUnmet(detail) {
    const d = detail || {};
    const lines = [];
    if (d.summary) {
      const s = d.summary;
      lines.push(`覆盖率 ${Math.round((s.coverage_ratio || 0) * 100)}%（目标 ≥95%）· 未测 ${s.untested} · 部分覆盖 ${s.partial} · 豁免 ${s.waived}`);
    }
    if (d.uncovered_high && d.uncovered_high.length) {
      lines.push(`高优先缺口 ${d.uncovered_high.length} 项：${d.uncovered_high.map((g) => `${g.test_type_name}@${g.target_value}`).join('、')}`);
    }
    if (d.depth_shortfall) lines.push(`深度短欠 ${d.depth_shortfall} 项`);
    if (d.untriaged_findings) lines.push(`未分诊 finding ${d.untriaged_findings} 项`);
    if (!lines.length) lines.push(JSON.stringify(d));
    const box = el('div', { class: 'alert-banner', style: 'white-space:pre-wrap;margin-bottom:10px' }, '未满足覆盖策略：\n' + lines.join('\n'));
    this.root.prepend(box);
  }
}
