const api = '/api/review/v1';
const $ = id => document.getElementById(id);
const drafts = new Map();
const deliveries = new Map();
let tasks = [];
let selected = '';
let active = false;
let saving = false;
let loading = false;
let scopeChosen = false;
const statusText = {
  SAVED: 'Salvo na web',
  APPLIED: 'Aplicado no desktop',
  WAITING_EXCEL: 'Aguardando o Excel ser fechado',
  CONFLICT: 'Requer conferência no desktop',
};
const reasons = {
  SOURCE_CHANGED: 'O valor, a revisão local ou a origem mudou.',
  UNREADABLE: 'O recorte ficou ilegível. O valor anterior foi preservado.',
  LOCAL_VALIDATION: 'O desktop encontrou um problema na validação.',
};

function node(tag, text, parent, className = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  item.className = className;
  parent?.append(item);
  return item;
}

function button(label, parent, action, style = 'secondary') {
  const item = node('button', label, parent, style);
  item.type = 'button';
  item.onclick = action;
  return item;
}

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  };
  const response = await fetch(api + path, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const problem = new Error(typeof error.detail === 'string'
      ? error.detail : 'O serviço não respondeu. Tente novamente.');
    problem.status = response.status;
    throw problem;
  }
  return response.json();
}

function editorDraft(task, card) {
  const id = `${task.id}/${card.id}`;
  if (!drafts.has(id)) drafts.set(id, {state: 'READABLE', transcription: '', operational_value: ''});
  return drafts.get(id);
}

function textInput(label, parent, draft, key, options = {}) {
  const item = node('input', '', node('label', label, parent));
  item.type = 'text';
  item.autocomplete = 'off';
  item.spellcheck = false;
  item.maxLength = key === 'transcription' ? 80 : 24;
  item.inputMode = options.inputMode || 'text';
  item.placeholder = options.placeholder || '';
  item.value = draft[key];
  item.oninput = () => { draft[key] = item.value; };
  return item;
}

function timeDigits(value) {
  return value.replace(/[^0-9]/g, '');
}

function formatTime(value, complete = false) {
  const singleHour = value.trim().match(/^([0-9])\s*[:.,hH]\s*([0-9]{2})$/);
  let digits = singleHour ? `0${singleHour[1]}${singleHour[2]}` : timeDigits(value).slice(0, 4);
  if (complete && digits.length === 3 && Number(digits.slice(1)) < 60) digits = `0${digits}`;
  return digits.length < 2 ? digits : `${digits.slice(0, 2)}:${digits.slice(2)}`;
}

function timeInput(label, parent, draft) {
  const item = textInput(label, parent, draft, 'operational_value', {placeholder: 'HH:MM', inputMode: 'numeric'});
  item.oninput = event => {
    let value = item.value;
    let position = timeDigits(value.slice(0, item.selectionStart)).length;
    if (event.inputType?.startsWith('delete') && draft.operational_value.includes(':') &&
        !value.includes(':') && value === timeDigits(draft.operational_value)) {
      if (event.inputType === 'deleteContentBackward') position--;
      value = value.slice(0, position) + value.slice(position + 1);
    }
    const formatted = formatTime(value);
    if (formatted !== item.value) {
      item.value = formatted;
      const cursor = Math.min(formatted.length, position + Number(position >= 2));
      item.setSelectionRange(cursor, cursor);
    }
    draft.operational_value = item.value;
  };
  item.onblur = () => { item.value = draft.operational_value = formatTime(item.value, true); };
  return item;
}

function zoom(card) {
  $('zoom-label').textContent = card.label;
  $('zoom-image').src = `${api}/crops/${card.crop}`;
  $('zoom-image').alt = `Recorte de ${card.label}`;
  $('zoom').showModal();
}

function cardView(card, task, parent, actionButtons) {
  const box = node('div', '', parent, 'card');
  node('h3', card.label, box);
  const evidence = button('', box, () => zoom(card), 'crop');
  evidence.setAttribute('aria-label', `Ampliar recorte · ${card.label}`);
  const image = node('img', '', evidence);
  image.src = `${api}/crops/${card.crop}`;
  image.alt = `Recorte de ${card.label}`;
  const reading = task.scope === 'operational'
    ? `Valor interpretado ${card.value || '—'}`
    : `OCR ${card.raw || 'sem leitura'} · Valor interpretado ${card.value || '—'}`;
  if (task.scope !== 'operational' || card.value) node('p', reading, box, 'ocr');
  const reviewed = task.reviews[card.id];
  if (reviewed) {
    node('p', statusText[reviewed.status], box, `status ${reviewed.status.toLowerCase()}`);
    const judgment = {EMPTY: 'Vazio confirmado', UNREADABLE: 'Ilegível'};
    node('p', reviewed.state === 'READABLE'
      ? `${reviewed.transcription} → ${reviewed.operational_value}` : judgment[reviewed.state], box, 'details');
    if (reasons[reviewed.reason]) node('p', reasons[reviewed.reason], box, 'details');
    return;
  }

  const draft = editorDraft(task, card);
  const fields = node('div', '', box, 'card-fields');
  const judgment = node('select', '', node('label', 'Leitura', fields));
  judgment.append(new Option('Legível', 'READABLE'), new Option('Vazio confirmado', 'EMPTY'),
    new Option('Ilegível', 'UNREADABLE'));
  judgment.value = draft.state;
  const isTime = task.kind === 'time';
  const literal = textInput('Transcrição literal', fields, draft, 'transcription');
  const operational = isTime ? timeInput('Valor operacional', fields, draft)
    : textInput('Valor operacional', fields, draft, 'operational_value', {inputMode: 'decimal'});
  const change = () => {
    draft.state = judgment.value;
    literal.disabled = operational.disabled = draft.state !== 'READABLE';
  };
  judgment.onchange = change;
  change();
  const feedback = node('p', '', box, 'save-feedback');
  feedback.setAttribute('role', 'status');
  actionButtons.push(button(isTime ? 'Salvar individual' : 'Salvar célula', box,
    () => submit(task, {[card.id]: entry(draft)}, actionButtons, feedback),
    isTime ? 'secondary save' : 'primary save'));
  return {judgment, literal};
}

function entry(draft, common = draft.operational_value) {
  return {
    state: draft.state,
    transcription: draft.state === 'READABLE' ? draft.transcription : '',
    operational_value: draft.state === 'READABLE' ? common : '',
  };
}

function taskView(task, parent) {
  const block = node('article', '', parent, 'task surface');
  const header = node('div', '', block, 'task-header');
  node('h2', task.title, header);
  const actions = [];
  const canPair = task.kind === 'time' && task.cards.length === 2 && Object.keys(task.reviews).length === 0;
  let common, pairFeedback, pairButton;
  if (canPair) {
    const group = node('div', '', header, 'group-actions');
    if (!drafts.has(task.id)) drafts.set(task.id, {operational_value: ''});
    common = timeInput('Horário comum', group, drafts.get(task.id));
    pairButton = button('Salvar para os dois', group, null, 'primary');
    actions.push(pairButton);
    pairFeedback = node('p', '', group, 'save-feedback');
    pairFeedback.setAttribute('role', 'status');
    node('p', 'Para salvar juntos, preencha o horário comum e a transcrição de cada recorte. Use os valores individuais para salvar separadamente.', block, 'task-hint');
  }
  const cards = node('div', '', block, 'cards');
  const controls = task.cards.map(card => cardView(card, task, cards, actions));
  if (canPair) {
    const showMissing = (control, message) => {
      pairFeedback.textContent = message;
      control.setCustomValidity(message);
      control.reportValidity();
      control.setCustomValidity('');
    };
    pairButton.onclick = () => {
      if (!common.value.trim()) {
        showMissing(common, 'Informe o horário comum dos dois mergulhadores.');
        return;
      }
      const unreadable = controls.findIndex(control => control.judgment.value !== 'READABLE');
      if (unreadable !== -1) {
        showMissing(controls[unreadable].judgment, 'Para salvar juntos, os dois recortes precisam estar legíveis.');
        return;
      }
      const missing = controls.findIndex(control => !control.literal.value.trim());
      if (missing !== -1) {
        showMissing(controls[missing].literal, `Preencha a transcrição do ${task.cards[missing].label.toLowerCase()}.`);
        return;
      }
      const entries = Object.fromEntries(task.cards.map((card, index) => [
        card.id, {state: 'READABLE', transcription: controls[index].literal.value, operational_value: common.value},
      ]));
      submit(task, entries, actions, pairFeedback);
    };
  }
}

function category(review) {
  if (!review) return 'open';
  if (review.status === 'CONFLICT') return 'conflict';
  return review.status === 'APPLIED' ? 'applied' : 'saved';
}

function visibleTasks() {
  return scopedTasks().filter(task => (!$('kind').value || task.kind === $('kind').value) &&
    (!$('field').value || task.field === $('field').value) &&
    ($('filter-state').value === 'all' ||
      task.cards.some(card => category(task.reviews[card.id]) === $('filter-state').value)));
}

function scopedTasks() {
  return tasks.filter(task => (task.scope || 'demo') === $('scope').value);
}

function render() {
  const hasOperational = tasks.some(task => task.scope === 'operational');
  $('scope').options[1].disabled = !hasOperational;
  if (!hasOperational) $('scope').value = 'demo';
  else if (!scopeChosen) {
    $('scope').value = 'operational';
    scopeChosen = true;
  }
  const counts = {open: 0, saved: 0, applied: 0, conflict: 0};
  for (const task of scopedTasks()) {
    for (const card of task.cards) counts[category(task.reviews[card.id])]++;
  }
  for (const [key, count] of Object.entries(counts)) $(`count-${key}`).textContent = count;
  const fields = new Map(scopedTasks().filter(task => !$('kind').value || task.kind === $('kind').value)
    .map(task => [task.field, task.title.split(' · ')[0]]));
  const field = $('field').value;
  $('field').replaceChildren(new Option('Todos os campos', ''), ...[...fields].map(([id, name]) => new Option(name, id)));
  $('field').value = fields.has(field) ? field : '';

  const workbench = $('workbench');
  workbench.replaceChildren();
  const matches = visibleTasks();
  if (!matches.length) {
    node('p', tasks.length ? 'Nenhum item corresponde aos filtros.' : 'Aguardando o desktop enviar as tarefas do piloto.',
      workbench, 'empty surface');
    return;
  }
  const index = Math.max(0, matches.findIndex(task => task.id === selected));
  selected = matches[index].id;
  const nav = node('div', '', workbench, 'task-nav');
  node('span', `${index + 1} de ${matches.length} blocos`, nav);
  const buttons = node('div', '', nav);
  button('Anterior', buttons, () => { selected = matches[index - 1].id; render(); }).disabled = index === 0;
  button('Próximo', buttons, () => { selected = matches[index + 1].id; render(); }).disabled = index === matches.length - 1;
  taskView(matches[index], workbench);
}

function operationId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

async function submit(task, entries, buttons, feedback) {
  if (saving) return;
  const body = {version: task.version, generation: task.generation, entries};
  const fingerprint = JSON.stringify(body);
  const previous = deliveries.get(task.id);
  saving = true;
  buttons.forEach(item => { item.disabled = true; });
  $('notice').textContent = '';
  feedback.textContent = 'Salvando…';
  try {
    const id = previous?.fingerprint === fingerprint ? previous.id : operationId();
    deliveries.set(task.id, {id, fingerprint});
    const result = await request(`/tasks/${task.id}/reviews`, {...body, id});
    const previousList = visibleTasks();
    const index = previousList.findIndex(item => item.id === task.id);
    task.generation += 1;
    for (const [slot, review] of Object.entries(result.entries)) {
      task.reviews[slot] = {...review, status: result.status};
      drafts.delete(`${task.id}/${slot}`);
    }
    deliveries.delete(task.id);
    if (!visibleTasks().some(item => item.id === task.id)) selected = previousList[index + 1]?.id || '';
    render();
    $('notice').textContent = `${statusText[result.status]} · ${task.title}`;
    $('notice').scrollIntoView({block: 'nearest'});
  } catch (error) {
    feedback.textContent = error.status ? error.message : 'Não foi possível enviar. Seu preenchimento foi mantido. Tente salvar de novo.';
  } finally {
    saving = false;
    buttons.forEach(item => { item.disabled = false; });
  }
}

async function refresh(showFeedback = false) {
  if (saving || loading) return;
  loading = true;
  try {
    const result = await request('/tasks');
    if (!saving && JSON.stringify(result.tasks) !== JSON.stringify(tasks)) {
      tasks = result.tasks;
      render();
    }
    if (showFeedback) $('notice').textContent = 'Fila atualizada.';
  } catch (error) {
    if (error.status === 401) {
      $('notice').textContent = 'Sua sessão expirou. Entre para continuar.';
      $('sign-in').hidden = false;
    } else {
      $('notice').textContent = error.status ? error.message : 'Serviço indisponível. Tente atualizar a fila.';
    }
  } finally {
    loading = false;
  }
}

for (const id of ['scope', 'kind', 'field', 'filter-state']) $(id).onchange = render;
$('refresh').onclick = () => refresh(true);
$('logout').onclick = async () => {
  try {
    await request('/logout', {});
    location.reload();
  } catch {
    $('notice').textContent = 'Não foi possível sair. Tente novamente.';
  }
};

async function start() {
  try {
    const session = await request('/session');
    active = true;
    $('reviewer').textContent = session.reviewer;
    $('account').hidden = $('queue').hidden = $('refresh').hidden = false;
    $('notice').textContent = '';
    render();
    await refresh();
  } catch (error) {
    $('notice').textContent = error.status === 401 ? '' : 'O serviço está indisponível. Recarregue a página para tentar novamente.';
    $('sign-in').hidden = false;
  }
}

start();
setInterval(() => {
  if (active && !document.hidden && !$('workbench').contains(document.activeElement)) refresh();
}, 20000);
