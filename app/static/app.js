let operationPending = false;
let lockedButtons = [];
const busyDialog = document.createElement('dialog');
busyDialog.className = 'busy-dialog';
busyDialog.setAttribute('aria-labelledby', 'busy-message');
busyDialog.innerHTML = '<div class="busy-spinner" aria-hidden="true"></div><h2 id="busy-message" role="status" aria-live="polite"></h2><p>请稍候，完成后将自动恢复操作</p>';
document.body.append(busyDialog);
busyDialog.addEventListener('cancel', event => event.preventDefault());

function lockPage(message) {
  if (operationPending) return false;
  operationPending = true;
  lockedButtons = [...document.querySelectorAll('.transaction-form button:not(:disabled)')];
  lockedButtons.forEach(button => { button.disabled = true; });
  busyDialog.querySelector('h2').textContent = message;
  document.body.setAttribute('aria-busy', 'true');
  document.body.classList.add('operation-pending');
  busyDialog.showModal();
  return true;
}

function unlockPage() {
  busyDialog.close();
  document.body.removeAttribute('aria-busy');
  document.body.classList.remove('operation-pending');
  lockedButtons.forEach(button => { button.disabled = false; });
  lockedButtons = [];
  operationPending = false;
}

// Covers Enter-key submissions and repeated submit events, not just mouse clicks.
document.addEventListener('submit', event => {
  if (operationPending) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);
window.addEventListener('pageshow', event => {
  if (event.persisted) {
    lockPage('正在刷新记录…');
    window.location.reload(); // Do not reuse a submitted form restored from bfcache.
  }
});

async function requestJson(url, body, timeoutMessage = '请求超时，请稍后重试；重复提交同一笔不会重复入账') {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(url, {
      method: 'POST', body, headers: {'Accept': 'application/json'}, signal: controller.signal,
    });
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('登录已失效或服务器响应异常，请确认登录后重试');
    }
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '提交内容无效，请检查后重试');
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(timeoutMessage);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function submitTransaction(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (operationPending) return;
  const body = new FormData(form);
  let status = form.querySelector('.submit-status');
  if (!status) {
    status = document.createElement('p');
    status.className = 'submit-status error';
    status.setAttribute('role', 'alert');
    form.append(status);
  }
  status.textContent = '';
  if (!lockPage('正在保存记录…')) return;
  let navigating = false;
  try {
    const result = await requestJson(form.action, body);
    const prefix = document.documentElement.dataset.basePath || '';
    if (typeof result.redirect_url !== 'string' || !result.redirect_url.startsWith(`${prefix}/?month=`)) {
      throw new Error('服务器响应异常，请刷新确认是否已保存');
    }
    busyDialog.querySelector('h2').textContent = '已保存，正在更新记录…';
    window.location.assign(result.redirect_url);
    navigating = true; // Keep the old page locked until the refreshed records load.
  } catch (error) {
    status.textContent = error instanceof TypeError ? '网络异常，请重试；同一笔不会重复入账' : error.message;
  } finally {
    if (!navigating) unlockPage();
  }
}

document.querySelectorAll('.chart-tabs button').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.chart-tabs button,.chart-panel').forEach(element => element.classList.remove('active'));
  button.classList.add('active');
  document.getElementById(button.dataset.chart).classList.add('active');
}));

function syncKind(form) {
  const selected = form.querySelector('input[name="kind"]:checked');
  if (!selected) return;
  const income = selected.value === 'income';
  const expenseType = form.querySelector('[data-role="expense-category"]');
  const incomeType = form.querySelector('[data-role="income-category"]');
  expenseType.disabled = income;
  expenseType.hidden = income;
  incomeType.disabled = !income;
  incomeType.hidden = !income;
  form.querySelector('[data-role="category-label"]').textContent = income ? '收入类型' : '支出类型';
  form.querySelector('[data-role="source-label"]').textContent = income ? '资金去向' : '资金来源';
}

async function runOcr(form) {
  if (operationPending) return;
  const imageInput = form.querySelector('[data-role="image-input"]');
  const button = form.querySelector('[data-role="ocr-button"]');
  const status = form.querySelector('[data-role="ocr-status"]');
  const note = form.querySelector('[data-role="note-input"]');
  const ocrText = form.querySelector('[data-role="ocr-text"]');
  status.className = 'ocr-status';
  if (!imageInput.files.length) {
    status.textContent = '请先选择图片';
    status.classList.add('error');
    return;
  }
  if (!lockPage('正在识别图片文字…')) return;
  button.textContent = '识别中…';
  status.textContent = '';
  const body = new FormData();
  body.append('image', imageInput.files[0]);
  try {
    const result = await requestJson(`${document.documentElement.dataset.basePath || ''}/ocr`, body,
      '识别等待超时，请稍后重试；后台可能仍在处理图片');
    if (typeof result.text !== 'string' || !result.text.trim()) throw new Error('未识别到文字，请换一张清晰图片');
    const recognized = result.text.trim();
    note.value += (note.value ? '\n' : '') + recognized;
    ocrText.value += (ocrText.value ? '\n' : '') + recognized;
    status.textContent = '已添加到备注';
    status.classList.add('success');
  } catch (error) {
    status.textContent = error instanceof TypeError ? '网络异常，请重试' : error.message;
    status.classList.add('error');
  } finally {
    button.textContent = '图转文';
    unlockPage();
  }
}

document.querySelectorAll('.transaction-form').forEach(form => {
  form.addEventListener('submit', submitTransaction);
  form.querySelectorAll('input[name="kind"]').forEach(input => input.addEventListener('change', () => syncKind(form)));
  form.querySelector('[data-role="ocr-button"]').addEventListener('click', () => runOcr(form));
  syncKind(form);
});

document.querySelectorAll('[data-open-dialog]').forEach(button => button.addEventListener('click', event => {
  event.stopPropagation();
  document.getElementById(button.dataset.openDialog).showModal();
}));
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
document.querySelectorAll('[data-record]').forEach(record => record.addEventListener('click', event => {
  if (event.target.closest('button,a,form,details')) return;
  document.querySelectorAll('[data-record].actions-visible').forEach(other => { if (other !== record) other.classList.remove('actions-visible'); });
  record.classList.toggle('actions-visible');
}));
document.querySelectorAll('.delete-record').forEach(form => form.addEventListener('submit', event => {
  if (!window.confirm('确定删除这条记录吗？此操作无法撤销。')) event.preventDefault();
}));
