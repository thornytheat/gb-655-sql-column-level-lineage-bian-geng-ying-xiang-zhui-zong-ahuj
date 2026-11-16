/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const revision = button.dataset.newSchemaId;
    if (revision) {
      body.new_schema_id = revision;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'run-revision'(button) {
    const body = {};
    const options = button.closest('[data-run-options]');
    if (options) {
      options.querySelectorAll('select[data-role]').forEach((select) => {
        if (select.value) body[`${select.dataset.role}_schema_id`] = select.value;
      });
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async amend(button) {
    const schemaId = button.dataset.schemaId;
    const items = [];
    document.querySelectorAll('[data-amend-table] tbody tr').forEach((row) => {
      const value = (name) => {
        const node = row.querySelector(`[data-field="${name}"]`);
        return node ? node.value.trim() : '';
      };
      items.push({
        item_id: row.dataset.itemId,
        action: value('action') || 'amend',
        table: value('table'),
        column: value('column'),
        type: value('type'),
        required: value('required'),
        field_id: value('field_id'),
      });
    });
    if (items.length === 0) throw new Error('没有可提交的补正项');
    const result = await request('/api/schemas/amend', {
      schema_id: schemaId,
      expected_revision_seq: Number(button.dataset.revisionSeq || 0),
      items,
    });
    renderAmendResult(result);
    return result.reused
      ? `命中已有补正单 ${result.amendment_id}`
      : `补正单 ${result.amendment_id}：补正 ${result.summary.amended || 0} 项`;
  },

  async resume(button) {
    const result = await request(
      `/api/schemas/amendments/${encodeURIComponent(button.dataset.amendmentId)}/resume`,
      {},
    );
    renderAmendResult(result);
    return `续跑 ${result.amendment_id}：${result.status}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },
};

const AMEND_STATUS = {
  applied: '已应用',
  partial: '部分应用',
  pending: '待续跑',
  failed: '失败',
  rejected: '未采纳',
  skipped: '保留隔离',
  amended: '已补正',
};

const ITEM_STATUS = {
  amended: '已补正',
  rejected: '已拒绝',
  skipped: '保留隔离',
  pending: '待处理',
};

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function renderAmendResult(result) {
  const target = document.querySelector('[data-amend-result]');
  if (!target) return;
  const summary = result.summary || {};
  const items = result.items || [];
  const rows = items.map((item) => {
    const original = item.original || {};
    const changed = item.input || {};
    const outcome = item.result
      ? `${item.result.table}.${item.result.column} ${item.result.type}`
      : (item.detail || '—');
    return `<tr>
      <td class="name">${escapeHtml(item.item_id)}</td>
      <td class="reason"><span class="code">${escapeHtml(original.reason_label || original.reason || '—')}</span></td>
      <td class="name">${escapeHtml([changed.table, changed.column, changed.type].filter(Boolean).join(' ') || '—')}</td>
      <td>${escapeHtml(ITEM_STATUS[item.status] || item.status)}</td>
      <td class="reason">${escapeHtml(outcome)}</td>
      <td class="reason">${escapeHtml(item.reason_label || item.reason || '—')}</td>
    </tr>`;
  }).join('');
  target.innerHTML = `
    <p class="lede">补正单 <span class="mono">${escapeHtml(result.amendment_id)}</span>
      状态 ${escapeHtml(AMEND_STATUS[result.status] || result.status)}
      修订 <span class="mono">r${escapeHtml(result.revision_seq || '—')}</span>
      结果版本 <span class="mono">${escapeHtml(result.result_schema_id || '—')}</span>
      补正 <span class="mono">${summary.amended || 0}</span>
      拒绝 <span class="mono">${summary.rejected || 0}</span>
      保留 <span class="mono">${summary.skipped || 0}</span></p>
    ${rows ? `<div class="wrap-scroll"><table><thead><tr><th>隔离项</th><th>原隔离原因</th>
      <th>修订内容</th><th>结果</th><th>落位</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : '<p class="empty">没有逐项结果。</p>'}`;
}

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name === 'run-revision' || name === 'resume') {
      setTimeout(() => window.location.reload(), 600);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
