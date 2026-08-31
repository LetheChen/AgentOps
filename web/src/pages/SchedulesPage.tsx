import { useState, useEffect, useCallback, useMemo } from 'react';
import { apiClient, type ScheduleInfo } from '../lib/api';

/**
 * 统一定时计划页（DESIGN_config_credential_refactor_v1 §5）。
 * 所有 cron 定时任务（日志拉取/工作流巡检/任务调度）统一在 config/schedules.yaml 管理：
 * 一个菜单、一组 API（/api/schedules）、一个配置文件段。
 *
 * id 策略：每条计划有一个不可变主键 `id`（新建时由后端按 name slug 自动生成）；
 *         `name` 是展示字段，可任意改。前端把 ID 输入框在编辑模式下锁死。
 */

// ── cron 解析（语义对齐后端 patroller._parse_cron_field / cron_match）──

function parseCronField(expr: string, minVal: number, maxVal: number): Set<number> {
  const result = new Set<number>();
  for (const raw of expr.split(',')) {
    const part = raw.trim();
    if (part === '*') {
      for (let v = minVal; v <= maxVal; v++) result.add(v);
    } else if (part.includes('/')) {
      const [base, stepStr] = part.split('/', 2);
      const step = parseInt(stepStr, 10);
      let start: number, end: number;
      if (base === '*' || base === '') {
        start = minVal; end = maxVal;
      } else if (base.includes('-')) {
        const [lo, hi] = base.split('-', 2);
        start = parseInt(lo, 10); end = parseInt(hi, 10);
      } else {
        start = parseInt(base, 10); end = maxVal;
      }
      for (let v = start; v <= end; v += step) result.add(v);
    } else if (part.includes('-')) {
      const [lo, hi] = part.split('-', 2);
      for (let v = parseInt(lo, 10); v <= parseInt(hi, 10); v++) result.add(v);
    } else {
      result.add(parseInt(part, 10));
    }
  }
  return result;
}

// 语法 + 边界校验（返回错误消息；null = 通过）。与后端 schedules_admin._validate_cron 对齐
function validateCron(expr: string): string | null {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return 'cron 必须是 5 个字段：分 时 日 月 周';
  const bounds: Array<[number, number]> = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];
  const fieldNames = ['分', '时', '日', '月', '周'];
  for (let i = 0; i < 5; i++) {
    const field = fields[i];
    if (!/^[0-9*,\-/]+$/.test(field)) return `${fieldNames[i]}字段含非法字符：${field}`;
    const [blo, bhi] = bounds[i];
    for (const part of field.split(',')) {
      const p = part.trim();
      if (p === '*' || p === '') continue;
      let base = p;
      if (p.includes('/')) {
        const [b, s] = p.split('/', 2);
        if (!/^\d+$/.test(s) || parseInt(s, 10) < 1) return `${fieldNames[i]}字段步长非法：${p}`;
        base = b;
      }
      if (base === '*' || base === '') continue;
      const nums = base.split('-').map(Number);
      if (nums.some((n) => Number.isNaN(n))) return `${fieldNames[i]}字段含非数字：${p}`;
      if (nums.length > 2) return `${fieldNames[i]}字段区间格式非法：${p}`;
      if (nums.length === 2 && nums[0] > nums[1]) return `${fieldNames[i]}字段区间起始大于结束：${p}`;
      const lo = Math.min(...nums), hi = Math.max(...nums);
      if (lo < blo || hi > bhi) return `${fieldNames[i]}字段越界 ${p}（合法 ${blo}-${bhi}）`;
    }
  }
  return null;
}

// 下次触发时间（从 from 起逐分钟匹配，上限 5 年；语义对齐后端 cron_match：五字段 AND、7=周日）
function nextCronRun(expr: string, from: Date): Date | null {
  if (validateCron(expr)) return null;
  const fields = expr.trim().split(/\s+/);
  const minuteSet = parseCronField(fields[0], 0, 59);
  const hourSet = parseCronField(fields[1], 0, 23);
  const domSet = parseCronField(fields[2], 1, 31);
  const monthSet = parseCronField(fields[3], 1, 12);
  const dowSet = parseCronField(fields[4], 0, 7);
  if (dowSet.has(7)) dowSet.add(0);

  const t = new Date(from.getTime());
  t.setSeconds(0, 0);
  t.setMinutes(t.getMinutes() + 1); // 从下一分钟起找
  const limit = new Date(from.getTime() + 5 * 365 * 24 * 60 * 60 * 1000);
  while (t <= limit) {
    if (
      minuteSet.has(t.getMinutes()) && hourSet.has(t.getHours()) &&
      domSet.has(t.getDate()) && monthSet.has(t.getMonth() + 1) &&
      dowSet.has(t.getDay())
    ) {
      return new Date(t);
    }
    t.setMinutes(t.getMinutes() + 1);
  }
  return null;
}

function fmtDateTime(d: Date | null): string {
  if (!d) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtIso(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : fmtDateTime(d);
}

// ── 常用 cron 预设 ──
const CRON_PRESETS: Array<{ label: string; expr: string }> = [
  { label: '每 5 分钟', expr: '*/5 * * * *' },
  { label: '每小时', expr: '0 * * * *' },
  { label: '每天 02:00', expr: '0 2 * * *' },
  { label: '每周一 09:00', expr: '0 9 * * 1' },
  { label: '每月 1 日 03:00', expr: '0 3 1 * *' },
];

// 编辑表单状态（id 编辑时锁死，新建时由后端按 name 自动生成）
interface ScheduleForm {
  id: string;            // 编辑模式有值且不可改；新建模式为空
  name: string;
  workflow_id: string;
  cron: string;
  enabled: boolean;
  inputs_text: string; // JSON 文本域
}

const EMPTY_FORM: ScheduleForm = {
  id: '', name: '', workflow_id: '', cron: '0 * * * *', enabled: true, inputs_text: '{}',
};

export function SchedulesPage() {
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([]);
  const [workflows, setWorkflows] = useState<Array<{ id: string; name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');

  // 编辑弹窗（null 关闭；'new' 新增；其余 = 编辑该 id）
  const [modal, setModal] = useState<'new' | string | null>(null);
  const [form, setForm] = useState<ScheduleForm>(EMPTY_FORM);
  const [modalError, setModalError] = useState('');
  const [saving, setSaving] = useState(false);
  // 删除确认（按 id）
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  // 行内启用切换进行中（按 id）
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [sched, wf] = await Promise.all([
        apiClient.listSchedules(),
        apiClient.getWorkflows().catch(() => ({ workflows: [] as Array<Record<string, unknown>> })),
      ]);
      setSchedules(sched.schedules || []);
      setWorkflows(
        (wf.workflows || []).map((w: Record<string, unknown>) => ({
          id: String(w.workflow_id || ''),
          name: String(w.name || w.workflow_id || ''),
        })),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(`加载定时计划失败：${msg}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openModal = useCallback((sc?: ScheduleInfo) => {
    if (sc) {
      setForm({
        id: sc.id,
        name: sc.name, workflow_id: sc.workflow_id, cron: sc.cron, enabled: sc.enabled,
        inputs_text: JSON.stringify(sc.inputs || {}, null, 2),
      });
      setModal(sc.id);
    } else {
      setForm(EMPTY_FORM);
      setModal('new');
    }
    setModalError('');
  }, []);

  // 弹窗内实时预览：cron 校验 + 下次触发时间
  const cronError = useMemo(
    () => (form.cron.trim() ? validateCron(form.cron) : null),
    [form.cron],
  );
  const cronPreview = useMemo(
    () => (form.cron.trim() && !cronError ? nextCronRun(form.cron, new Date()) : null),
    [form.cron, cronError],
  );

  const handleSave = useCallback(async () => {
    const f = form;
    const isNew = modal === 'new';
    if (isNew && !f.name.trim()) { setModalError('请输入计划名称'); return; }
    if (!/^[\u4e00-\u9fa5\u3400-\u4dbf\uf900-\ufaffa-zA-Z0-9_\-]+$/.test(f.name.trim())) { setModalError('计划名称仅支持中文/字母/数字/下划线/连字符'); return; }
    if (/\s/.test(f.name)) { setModalError('计划名称不允许包含空格'); return; }
    if (!f.workflow_id) { setModalError('请选择要定时运行的 workflow'); return; }
    const cronErr = validateCron(f.cron);
    if (cronErr) { setModalError(`cron 非法：${cronErr}`); return; }
    let inputs: Record<string, unknown>;
    try {
      const parsed = JSON.parse(f.inputs_text.trim() || '{}');
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new Error('必须是 JSON 对象（{}）');
      }
      inputs = parsed as Record<string, unknown>;
    } catch (e) {
      setModalError(`inputs 不是合法的 JSON 对象：${e instanceof Error ? e.message : String(e)}`);
      return;
    }

    setSaving(true);
    setModalError('');
    try {
      // 编辑模式带原 id（不可变）；新建模式不带 id（后端按 name slug 自动生成）
      await apiClient.upsertSchedule({
        id: isNew ? undefined : f.id,
        name: f.name.trim(), workflow_id: f.workflow_id, cron: f.cron.trim(),
        enabled: f.enabled, inputs,
      });
      await load();
      setModal(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setModalError(`保存失败：${msg}`);
    } finally {
      setSaving(false);
    }
  }, [form, modal, load]);

  const handleDelete = useCallback(async () => {
    if (!deleteId) return;
    setDeleting(true);
    try {
      await apiClient.deleteSchedule(deleteId);
      await load();
      setDeleteId(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setActionError(`删除计划失败：${msg}`);
      setDeleteId(null);
    } finally {
      setDeleting(false);
    }
  }, [deleteId, load]);

  // 行内快速启用/停用（upsert 全量字段，按 id）
  const handleToggle = useCallback(async (sc: ScheduleInfo) => {
    setTogglingId(sc.id);
    setActionError('');
    try {
      await apiClient.upsertSchedule({
        id: sc.id, name: sc.name, workflow_id: sc.workflow_id, cron: sc.cron,
        enabled: !sc.enabled, inputs: sc.inputs || {},
      });
      await load();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setActionError(`切换启用状态失败：${msg}`);
    } finally {
      setTogglingId(null);
    }
  }, [load]);

  const inputStyle = { width: '100%' };
  const labelStyle = { display: 'block', fontSize: '12px', color: 'var(--color-text-tertiary)', marginBottom: '4px' } as const;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* 页面标题 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: '20px', fontWeight: 600, color: 'var(--color-text-primary)' }}>定时计划</div>
          <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
            统一管理所有 cron 定时任务（日志拉取 / 工作流巡检 / 任务调度），写入 config/schedules.yaml
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button className="btn-secondary" onClick={load} disabled={loading}>
            {loading ? '加载中...' : '刷新'}
          </button>
          <button className="btn-primary" onClick={() => openModal()}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginRight: '4px' }}>
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            新增计划
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 12px', borderRadius: 'var(--radius-md)', fontSize: '13px', background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>
          {error}
        </div>
      )}
      {actionError && (
        <div style={{ padding: '10px 12px', borderRadius: 'var(--radius-md)', fontSize: '13px', background: 'var(--state-error-tint)', color: 'var(--state-error)' }}>
          {actionError}
        </div>
      )}

      {/* 计划表格 */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>ID</th>
              <th>Workflow</th>
              <th>cron</th>
              <th>下次触发</th>
              <th>启用</th>
              <th>inputs</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>正在加载...</td></tr>
            ) : schedules.length === 0 ? (
              <tr><td colSpan={8} style={{ textAlign: 'center', padding: '32px', color: 'var(--color-text-tertiary)' }}>暂无定时计划，点击右上角"新增计划"添加</td></tr>
            ) : (
              schedules.map((sc) => {
                const wfName = workflows.find((w) => w.id === sc.workflow_id)?.name;
                const inputKeys = Object.keys(sc.inputs || {});
                return (
                  <tr key={sc.id}>
                    <td className="font-mono" style={{ fontSize: '13px', color: 'var(--color-text-primary)' }}>{sc.name}</td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }} title={sc.id}>{sc.id}</td>
                    <td>
                      <div className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-primary)' }}>{sc.workflow_id}</div>
                      {wfName && wfName !== sc.workflow_id && (
                        <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)' }}>{wfName}</div>
                      )}
                    </td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{sc.cron}</td>
                    <td style={{ fontSize: '12px', color: sc.enabled ? 'var(--color-text-secondary)' : 'var(--color-text-tertiary)' }}>
                      {sc.enabled ? fmtIso(sc.next_run) : '（停用）'}
                    </td>
                    <td>
                      <button
                        onClick={() => handleToggle(sc)}
                        disabled={togglingId === sc.id}
                        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '4px', display: 'flex', alignItems: 'center', gap: '6px' }}
                        title={sc.enabled ? '点击停用' : '点击启用'}
                      >
                        <div className={`status-dot status-dot-${sc.enabled ? 'success' : 'neutral'}`} />
                        <span style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>
                          {togglingId === sc.id ? '…' : sc.enabled ? '已启用' : '停用'}
                        </span>
                      </button>
                    </td>
                    <td className="font-mono" style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={JSON.stringify(sc.inputs)}>
                      {inputKeys.length === 0 ? '—' : `${inputKeys.length} 个参数`}
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: '4px' }}>
                        <button className="btn-secondary btn-sm" onClick={() => openModal(sc)}>编辑</button>
                        <button
                          onClick={() => setDeleteId(sc.id)}
                          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-secondary)', padding: '4px', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center' }}
                          title="删除计划"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                          </svg>
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div style={{ fontSize: '13px', color: 'var(--color-text-tertiary)' }}>
        配置改动回写 config/schedules.yaml，Patroller 按统一调度循环触发；修改后无需重启后端（下轮巡检自动生效）。
      </div>

      {/* ── 计划编辑弹窗 ── */}
      {modal !== null && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setModal(null); }}
        >
          <div className="card-elevated" style={{ width: '640px', maxHeight: '86vh', overflowY: 'auto', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid var(--color-border-subtle)', position: 'sticky', top: 0, background: 'var(--color-bg-primary)' }}>
              <span style={{ fontSize: '16px', fontWeight: 600, color: 'var(--color-text-primary)' }}>
                {modal === 'new' ? '新增定时计划' : `编辑定时计划：${form.name || form.id}`}
              </span>
              <button onClick={() => setModal(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-tertiary)', padding: '4px', display: 'flex' }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            <div style={{ padding: '20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
              <div>
                <label style={labelStyle}>计划名称（展示字段，可任意改）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="致远OA日志巡检" autoFocus={modal === 'new'} />
              </div>
              <div>
                <label style={labelStyle}>计划 ID（不可变，新建自动生成）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.id}
                  onChange={(e) => setForm({ ...form, id: e.target.value })}
                  disabled={modal !== 'new'} placeholder="（新建后自动生成）" />
              </div>
              <div>
                <label style={labelStyle}>Workflow（定时运行的目标）</label>
                <select className="input-base font-mono" style={inputStyle} value={form.workflow_id}
                  onChange={(e) => setForm({ ...form, workflow_id: e.target.value })}>
                  <option value="">（请选择）</option>
                  {workflows.map((w) => (
                    <option key={w.id} value={w.id}>{w.id}{w.name !== w.id ? ` · ${w.name}` : ''}</option>
                  ))}
                </select>
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>cron 表达式（分 时 日 月 周，五字段）</label>
                <input className="input-base font-mono" style={inputStyle} value={form.cron}
                  onChange={(e) => setForm({ ...form, cron: e.target.value })}
                  placeholder="0 2 * * *" />
                <div style={{ display: 'flex', gap: '6px', marginTop: '6px', flexWrap: 'wrap' }}>
                  {CRON_PRESETS.map((p) => (
                    <button key={p.expr} type="button" className="filter-chip" style={{ fontSize: '11px', padding: '2px 10px' }}
                      onClick={() => setForm({ ...form, cron: p.expr })} title={p.expr}>
                      {p.label}
                    </button>
                  ))}
                </div>
                {cronError ? (
                  <div style={{ fontSize: '12px', color: 'var(--state-error)', marginTop: '6px' }}>{cronError}</div>
                ) : (
                  <div style={{ fontSize: '12px', color: 'var(--color-text-tertiary)', marginTop: '6px' }}>
                    下次触发：{cronPreview ? fmtDateTime(cronPreview) : '（无法计算）'}
                  </div>
                )}
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <label style={labelStyle}>inputs（workflow 入参，JSON 对象）</label>
                <textarea className="input-base font-mono" style={{ ...inputStyle, minHeight: '96px', resize: 'vertical' }}
                  value={form.inputs_text}
                  onChange={(e) => setForm({ ...form, inputs_text: e.target.value })}
                  placeholder={'{\n  "source_id": "prod-seeyon"\n}'} />
              </div>
              <div style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <input id="sched-enabled" type="checkbox" checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                <label htmlFor="sched-enabled" style={{ fontSize: '13px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
                  启用该计划（停用后保留配置但不触发）
                </label>
              </div>

              {modalError && (
                <div style={{ gridColumn: '1 / -1', fontSize: '13px', color: 'var(--state-error)', padding: '8px 12px', background: 'var(--state-error-tint)', borderRadius: 'var(--radius-md)' }}>
                  {modalError}
                </div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '12px 20px', borderTop: '1px solid var(--color-border-subtle)', position: 'sticky', bottom: 0, background: 'var(--color-bg-primary)' }}>
              <button className="btn-secondary btn-sm" onClick={() => setModal(null)}>取消</button>
              <button className="btn-primary btn-sm" onClick={handleSave} disabled={saving}>
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 删除确认 ── */}
      {deleteId && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={(e) => { if (e.target === e.currentTarget) setDeleteId(null); }}>
          <div className="card-elevated" style={{ width: '400px', boxShadow: 'var(--shadow-floating)' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ padding: '20px' }}>
              <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: '8px' }}>删除定时计划</div>
              <div style={{ fontSize: '13px', color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>
                确定删除 <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>{deleteId}</span> 吗？
                <br />删除后该任务不再定时触发，不可恢复。
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', padding: '0 20px 20px' }}>
              <button className="btn-secondary btn-sm" onClick={() => setDeleteId(null)} disabled={deleting}>取消</button>
              <button className="btn-sm" style={{ background: 'var(--state-error)', color: '#fff', border: 'none', borderRadius: 'var(--radius-md)', fontWeight: 600, fontSize: '13px', padding: '0 16px', height: '32px', cursor: 'pointer' }}
                onClick={handleDelete} disabled={deleting}>
                {deleting ? '删除中...' : '删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default SchedulesPage;