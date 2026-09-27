import {t as txt,tr,locale,getLanguage} from './i18n.js';
import {icon,active,statuses} from './ui.js';
import {renderMarkdown,timestamp,mergeItems,draftPayload,samePayload,submissionAttempt,composerLimit} from './conversation_format.js';

const deliveryLabels = {pending:'Ожидает внесения в план',planned:'В плане · предстоит выполнить',review_pending:'Выполнение ожидает проверки',
 verified:'Выполнение проверено',claimed:'Заявлено выполнение · без независимой проверки',queued:'Контекст ожидает передачи',delivered:'Контекст передан'};
const actorLabels = {executor:'Исполнитель',reviewer:'Рецензент',diagnostician:'Диагност'};
const toolStates = {running:'Выполняется',completed:'Выполнено',error:'Ошибка',failed:'Ошибка',cancelled:'Отменено'};
const draftKey = id => `agentvisor-message-draft:${id}`;
const freshDraft = () => ({text:'',kind:'instruction',recheck:false,attempt:null});
function readDraft(id) {
 try { const saved = JSON.parse(sessionStorage.getItem(draftKey(id))); return {...freshDraft(),...saved}; } catch { return freshDraft(); }
}
function saveDraft(id,draft) { try { sessionStorage.setItem(draftKey(id),JSON.stringify(draft)); } catch { /* Private browsing can deny storage. The in-memory draft remains. */ } }
function uuid() {
 if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
 return 'message-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
}
const setText = (node,value) => { if (node && node.textContent !== value) node.textContent = value; };

export function initConversation(ctx) {
 const find = id => document.getElementById(id);
 const dom = Object.fromEntries(['conversation-scroll','conversation-messages','conversation-empty','conversation-loading','conversation-error',
  'conversation-retry','conversation-older','conversation-latest','conversation-activity','message-form','message-text','message-kind','message-recheck',
  'message-send','message-help','message-count','message-error'].map(id => [id.replaceAll('-','_'),find(id)]));
 const records = new Map(); let currentId = '', currentTask = null;
 const record = id => {
  if (!records.has(id)) records.set(id,{id,items:[],nodes:new Map(),dates:new Map(),groups:new Map(),draft:readDraft(id),composer:{},loaded:false,
   loading:false,loadingOlder:false,sending:false,lastFetch:0,nextBefore:null,hasMore:false,loadError:'',sendError:'',scroll:0,unread:false});
  return records.get(id);
 };
 const selected = state => currentId === state.id;
 const bottom = () => dom.conversation_scroll.scrollHeight-dom.conversation_scroll.scrollTop-dom.conversation_scroll.clientHeight < 96;
 function scrollLatest() {
  dom.conversation_scroll.scrollTop = dom.conversation_scroll.scrollHeight;
  if (currentId) record(currentId).unread = false;
  dom.conversation_latest.hidden = true;
 }
 function resizeComposer() {
  dom.message_text.style.height = 'auto';
  dom.message_text.style.height = Math.min(220,Math.max(44,dom.message_text.scrollHeight))+'px';
  positionLatestButton();
 }
 function positionLatestButton() {
  const panel = dom.conversation_latest.parentElement?.getBoundingClientRect();
  if (!panel || panel.bottom <= panel.top) return;
  const composerTop = dom.message_form.getBoundingClientRect().top;
  const activityTop = dom.conversation_activity.hidden ? composerTop : dom.conversation_activity.getBoundingClientRect().top;
  dom.conversation_latest.style.bottom = Math.max(12,Math.ceil(panel.bottom-Math.min(composerTop,activityTop)+12))+'px';
 }
 if (typeof ResizeObserver !== 'undefined') {
  const observer = new ResizeObserver(positionLatestButton);
  observer.observe(dom.message_form); observer.observe(dom.conversation_activity);
 }
 function readComposer() {
  if (!currentId) return;
  const state = record(currentId), before = state.draft;
  state.draft = {text:dom.message_text.value,kind:dom.message_kind.value,recheck:dom.message_recheck.checked,attempt:before.attempt};
  if (!samePayload(before,state.draft)) state.sendError = '';
  saveDraft(currentId,state.draft); resizeComposer(); renderComposer();
 }
 function renderComposer() {
  const state = currentId ? record(currentId) : null;
  const limit = composerLimit(state?.composer,dom.message_text.value);
  const unavailable = !state || ['succeeded','completed_unverified'].includes(currentTask?.status);
  dom.message_text.disabled = unavailable;
  dom.message_kind.disabled = unavailable;
  dom.message_recheck.disabled = unavailable || dom.message_kind.value === 'reference';
  dom.message_send.disabled = unavailable || state.sending || !state.loaded || !limit.canSend;
  dom.message_send.setAttribute('aria-busy',String(!!state?.sending));
  dom.message_send.setAttribute('aria-label',txt(state?.sending ? 'Отправка сообщения' : 'Отправить сообщение'));
  const label = dom.message_send.querySelector('span'); if (label) setText(label,txt(state?.sending ? 'Отправка…' : 'Отправить'));
  setText(dom.message_count,limit.size ? `${limit.size.toLocaleString(locale())} / ${limit.maximum.toLocaleString(locale())}` : '');
  dom.message_count.classList.toggle('over-limit',limit.size > limit.maximum);
  let help = txt('Enter — отправить · Shift + Enter — новая строка');
  if (!state) help = txt('Выберите задачу или создайте новую, чтобы начать работу.');
  else if (unavailable) help = txt('Задача завершена. Создайте новую задачу для дальнейшей работы.');
  else if (limit.full) help = txt('Для этой задачи достигнут лимит сообщений. Измените цель, чтобы добавить требования.');
  else if (state.composer.paused || ['paused','stopped','blocked','failed','draft'].includes(currentTask?.status)) help = txt('Сообщение сохранится. Агент получит его после запуска или продолжения задачи.');
  else if (dom.message_kind.value === 'reference') help = txt('Контекст будет передан модели при следующем обращении.');
  else help = txt('Указание будет передано агенту и отслежено в плане.');
  setText(dom.message_help,help);
  setText(dom.message_error,state?.sendError || ''); dom.message_error.hidden = !state?.sendError;
 }
 function selectedTextWithin(node) {
  const selection = document.getSelection?.();
  return !!selection && !selection.isCollapsed && (node.contains(selection.anchorNode) || node.contains(selection.focusNode));
 }
 function renderBody(node,item) {
  const version = item.text + (item.kind === 'tool' ? JSON.stringify(item.details || {}) : '');
  if (node._version === version || selectedTextWithin(node)) return;
  node.innerHTML = renderMarkdown(item.text); node._version = version;
  if (item.kind === 'tool' && item.details && Object.keys(item.details).length) {
   const details = document.createElement('pre'); details.className = 'message-tool-details'; details.textContent = JSON.stringify(item.details,null,2); node.append(details);
  }
 }
 function createMessage(item) {
  const article = document.createElement('article'); article.dataset.messageId = item.id;
  const header = document.createElement('header'); header.className = 'message-header';
  const author = document.createElement('span'); author.className = 'message-author';
  const time = document.createElement('time'); time.className = 'message-time';
  header.append(author,time); article.append(header);
  const body = document.createElement('div'); body.className = 'message-body markdown-body';
  let disclosure = null, summary = null;
  if (['tool','reasoning'].includes(item.kind)) {
   disclosure = document.createElement('details'); disclosure.className = 'message-disclosure';
   summary = document.createElement('summary'); disclosure.append(summary,body); article.append(disclosure);
   disclosure.addEventListener('toggle',() => { if (disclosure.open) renderBody(body,article._item); });
  } else article.append(body);
  const footer = document.createElement('footer'); footer.className = 'message-footer';
  const state = document.createElement('span'); state.className = 'message-state';
  const steps = document.createElement('span'); steps.className = 'message-steps';
  const journal = document.createElement('button'); journal.type = 'button'; journal.className = 'message-event-link';
  journal.addEventListener('click',() => document.dispatchEvent(new CustomEvent('conversation-event',{detail:{taskId:currentId,eventId:article._item.details?.first_event_id}})));
  const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'message-copy'; copy.innerHTML = icon('copy');
  copy.addEventListener('click',async () => {
   try {
    await navigator.clipboard.writeText(article._item.text || '');
    copy.setAttribute('aria-label',txt('Скопировано')); copy.title = txt('Скопировано'); copy.innerHTML = icon('check');
    setTimeout(() => { copy.innerHTML = icon('copy'); copy.title = txt('Копировать сообщение'); copy.setAttribute('aria-label',copy.title); },1800);
   } catch { copy.title = txt('Не удалось скопировать. Выделите текст сообщения.'); copy.setAttribute('aria-label',copy.title); }
  });
  footer.append(state,steps,journal,copy); article.append(footer);
  article._parts = {author,time,body,disclosure,summary,state,steps,journal,copy}; return article;
 }
 function updateMessage(node,item) {
  // Older loaded pages are not fetched on every poll. Their delivery badges still
  // follow the task's current instruction receipts, never a stale page snapshot.
  const receipt = item.role === 'user' && currentTask?.user_instructions?.find(note => note.version === item.details?.version);
  if (receipt && (!currentTask.updated || timestamp(currentTask.updated) >= timestamp(item.updated_at))) item = {...item,state:receipt.state,step_ids:receipt.step_ids || []};
  node._item = item; const parts = node._parts;
  const role = ['user','assistant','system'].includes(item.role) ? item.role : 'system';
  node.className = `conversation-message message-${role}`; node.dataset.kind = item.kind;
  setText(parts.author,role === 'user' ? txt('Вы') : role === 'system' ? txt('Событие задачи') : txt(actorLabels[item.actor] || 'AgentVisor'));
  const date = new Date(timestamp(item.time));
  parts.time.dateTime = date.toISOString(); setText(parts.time,date.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'}));
  parts.time.title = date.toLocaleString(locale());
  if (parts.summary) setText(parts.summary,txt(item.kind === 'reasoning' ? 'Рассуждение модели' : 'Работа с инструментом') +
   (item.kind === 'tool' ? ' · '+String(item.text || '').split('\n')[0].slice(0,110) : ''));
  if (!parts.disclosure || parts.disclosure.open) renderBody(parts.body,item);
  const delivery = role === 'user' ? txt(deliveryLabels[item.state] || (item.kind === 'reference' ? 'Контекст' : 'Указание')) : item.kind === 'tool' ? txt(toolStates[item.state] || '') : '';
  setText(parts.state,delivery); parts.state.className = 'message-state '+(Object.hasOwn(deliveryLabels,item.state) ? item.state : '');
  parts.state.hidden = !delivery;
  parts.journal.hidden = !item.details?.truncated || !item.details?.first_event_id;
  setText(parts.journal,txt('Показан фрагмент · открыть журнал'));
  const stepIds = item.step_ids || [], stepKey = JSON.stringify(stepIds)+(currentTask?.checklist?.length || 0)+getLanguage();
  if (parts.steps.dataset.version !== stepKey) {
   parts.steps.replaceChildren();
   for (const id of stepIds) {
    const index = (currentTask?.checklist || []).findIndex(step => step.id === id), button = document.createElement('button');
    button.type = 'button'; button.className = 'message-step-link'; button.textContent = index >= 0 ? tr`Шаг ${index+1}` : txt('Пункт плана');
    button.addEventListener('click',() => document.dispatchEvent(new CustomEvent('conversation-step',{detail:{taskId:currentId,stepId:id}})));
    parts.steps.append(button);
   }
   parts.steps.dataset.version = stepKey;
  }
  parts.copy.title = txt('Копировать сообщение'); parts.copy.setAttribute('aria-label',parts.copy.title);
 }
 function syncChildren(parent,children) {
  for (let index = 0; index < children.length; index++) {
   if (parent.children[index] !== children[index]) parent.insertBefore(children[index],parent.children[index] || null);
  }
  while (parent.children.length > children.length) parent.lastElementChild.remove();
 }
 function workGroup(state,items,used) {
  const key = String(items[0].id);
  // Prepending an older page may extend an existing run. Reuse its element so
  // expanding history does not close a group or recreate selected messages.
  let group = state.groups.get(key);
  if (!group || used.has(group)) group = items.map(item => state.nodes.get(String(item.id))?._workGroup).find(node => node && !used.has(node));
  if (!group) {
   group = document.createElement('details'); group.className = 'conversation-work-group';
   const summary = document.createElement('summary'), label = document.createElement('span'), time = document.createElement('time');
   label.className = 'work-group-label'; time.className = 'work-group-time'; summary.append(label,time);
   const body = document.createElement('div'); body.className = 'conversation-work-items'; group.append(summary,body);
   group._parts = {label,time,body};
  }
  state.groups.set(key,group); used.add(group); group.dataset.workGroupId = key;
  const count = items.length, date = new Date(timestamp(items.at(-1).updated_at || items.at(-1).time));
  setText(group._parts.label,tr`Действия агента · ${count}`);
  setText(group._parts.time,date.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'})); group._parts.time.dateTime = date.toISOString();
  const children = items.map(item => {
   const id = String(item.id); if (!state.nodes.has(id)) state.nodes.set(id,createMessage(item));
   const node = state.nodes.get(id); node._workGroup = group; updateMessage(node,item); return node;
  });
  syncChildren(group._parts.body,children); return group;
 }
 function render(state,{older=false,initial=false}={}) {
  if (!selected(state)) return;
  const scroll = dom.conversation_scroll, stick = initial || (!older && bottom());
  const top = scroll.getBoundingClientRect().top;
  const visible = node => (node.getClientRects?.().length ?? 1) > 0 && node.getBoundingClientRect().bottom > top;
  const anchor = [...dom.conversation_messages.querySelectorAll('[data-message-id]')].find(visible) ||
   [...dom.conversation_messages.children].find(node => node.dataset.workGroupId && visible(node));
  const anchorTop = anchor?.getBoundingClientRect().top;
  const previousIds = new Set([...dom.conversation_messages.querySelectorAll('[data-message-id]')].map(node => node.dataset.messageId));
  const ordered = [], usedGroups = new Set(); let lastDate = '', work = [];
  const flushWork = () => { if (work.length) ordered.push(workGroup(state,work,usedGroups)); work = []; };
  for (const item of state.items) {
   const date = new Date(timestamp(item.time)), dateKey = date.toLocaleDateString('en-CA');
   if (dateKey !== lastDate) {
    flushWork();
    if (!state.dates.has(dateKey)) { const divider = document.createElement('div'); divider.className = 'conversation-date'; state.dates.set(dateKey,divider); }
    const divider = state.dates.get(dateKey);
    setText(divider,date.toLocaleDateString(locale(),{day:'numeric',month:'long',year:date.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined}));
    ordered.push(divider); lastDate = dateKey;
   }
   if (['tool','reasoning'].includes(item.kind)) { work.push(item); continue; }
   flushWork();
   const id = String(item.id); if (!state.nodes.has(id)) state.nodes.set(id,createMessage(item));
   const node = state.nodes.get(id); node._workGroup = null; updateMessage(node,item); ordered.push(node);
  }
  flushWork(); syncChildren(dom.conversation_messages,ordered);
  dom.conversation_empty.hidden = state.items.length > 0 || state.loading || !!state.loadError;
  dom.conversation_loading.hidden = state.loaded || !state.loading;
  dom.conversation_older.hidden = !state.hasMore;
  dom.conversation_older.disabled = state.loadingOlder;
  setText(dom.conversation_older,txt(state.loadingOlder ? 'Загрузка…' : 'Предыдущие сообщения'));
  dom.conversation_error.hidden = !state.loadError;
  const errorText = dom.conversation_error.querySelector('[data-error-text]');
  if (errorText) setText(errorText,state.loadError);
  else dom.conversation_error.setAttribute('aria-label',state.loadError);
  if (stick) scrollLatest();
  else {
   if (anchor?.isConnected) scroll.scrollTop += anchor.getBoundingClientRect().top-anchorTop;
   if (!older && state.items.some(item => !previousIds.has(String(item.id)))) state.unread = true;
   dom.conversation_latest.hidden = !state.unread;
  }
  renderComposer(); renderActivity();
 }
 function renderActivity() {
  const task = currentTask;
  let message = '';
  if (task && active.has(task.status)) {
   const role = task.active_role || task.runtime_state?.active_role, actor = typeof role === 'object' ? role?.name : role;
   message = actor === 'reviewer' || task.status === 'verifying' ? txt('Рецензент проверяет результат') :
    actor === 'diagnostician' || task.status === 'recovering' ? txt('Агент разбирается с затруднением') :
    task.generation_activity?.active ? txt('Модель формирует ответ') : txt('Агент работает над задачей');
  } else if (task?.status === 'paused') message = txt('Задача на паузе');
  else if (task && ['blocked','failed'].includes(task.status)) message = statuses[task.status];
  setText(dom.conversation_activity,message); dom.conversation_activity.hidden = !message;
  dom.conversation_activity.classList.toggle('working',!!task && active.has(task.status));
  positionLatestButton();
 }
 function renderEmpty() {
  setText(dom.conversation_empty.querySelector('h2'),txt(currentTask ? 'Переписка этой задачи' : 'Задача начинается с разговора'));
  setText(dom.conversation_empty.querySelector('p'),txt(currentTask ? 'Здесь будут ваши указания, ответы и результаты работы агента.' :
   'Задайте цель, уточняйте требования и следите за результатом в одной переписке.'));
  const create = dom.conversation_empty.querySelector('[data-page="tasks"]'); if (create) create.hidden = !!currentTask;
 }
 async function load(state,{older=false,force=false}={}) {
  if (state.loading || state.loadingOlder || (!force && !older && Date.now()-state.lastFetch < 2000)) return;
  if (older && !state.nextBefore) return;
  state.lastFetch = Date.now(); state[older ? 'loadingOlder' : 'loading'] = true;
  if (selected(state)) render(state);
  try {
   const query = new URLSearchParams({limit:'60'}); if (older) query.set('before',state.nextBefore);
   const response = await ctx.api(`/tasks/${encodeURIComponent(state.id)}/conversation?${query}`);
   const incoming = response.items || [], first = !state.loaded;
   const oldIds = new Set(state.items.map(item => String(item.id)));
   const gap = !older && state.items.length && incoming.length && !incoming.some(item => oldIds.has(String(item.id)));
   state.items = mergeItems(state.items,incoming);
   state.composer = response.composer || state.composer;
   if (first || older || gap) { state.nextBefore = response.next_before; state.hasMore = !!response.has_more; }
   state.loaded = true; state.loadError = '';
   render(state,{older,initial:first});
  } catch (error) {
   state.loadError = txt('Не удалось загрузить переписку. Повторите попытку.')+(error.message ? ' '+error.message : '');
  } finally {
   state.loading = false; state.loadingOlder = false;
   if (selected(state)) render(state,{older});
  }
 }
 async function send(event) {
  event?.preventDefault(); readComposer();
  if (!currentId) return;
  const state = record(currentId);
  if (state.sending || !state.loaded || !composerLimit(state.composer,state.draft.text).canSend || dom.message_text.disabled) return;
  const attempt = submissionAttempt(state.draft,uuid);
  state.draft.attempt = attempt; saveDraft(state.id,state.draft);
  state.sending = true; state.sendError = ''; renderComposer();
  try {
   await ctx.api(`/tasks/${encodeURIComponent(state.id)}/context`,{method:'POST',body:JSON.stringify(attempt)});
   if (samePayload(state.draft,attempt)) {
    state.draft = {...freshDraft(),kind:state.draft.kind}; saveDraft(state.id,state.draft);
    if (selected(state)) { dom.message_text.value = ''; dom.message_recheck.checked = false; resizeComposer(); }
   }
   state.sendError = '';
   await load(state,{force:true});
   if (selected(state)) { scrollLatest(); if (dom.message_form.contains(document.activeElement)) dom.message_text.focus({preventScroll:true}); }
   try { await ctx.refresh?.(); } catch { /* The message is already saved; a dashboard refresh must not turn it into a failed send. */ }
  } catch (error) {
   state.sendError = error.message || txt('Сообщение не отправлено. Текст сохранён — попробуйте ещё раз.');
  } finally { state.sending = false; if (selected(state)) renderComposer(); }
 }
 dom.message_form.addEventListener('submit',send);
 dom.message_text.addEventListener('input',readComposer);
 dom.message_text.addEventListener('keydown',event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!dom.message_send.disabled) send(); }
 });
 dom.message_kind.addEventListener('change',() => { if (dom.message_kind.value === 'reference') dom.message_recheck.checked = false; readComposer(); });
 dom.message_recheck.addEventListener('change',readComposer);
 dom.conversation_retry.addEventListener('click',() => { if (currentId) load(record(currentId),{force:true}); });
 dom.conversation_older.addEventListener('click',() => { if (currentId) load(record(currentId),{older:true}); });
 dom.conversation_latest.addEventListener('click',scrollLatest);
 dom.conversation_scroll.addEventListener('scroll',() => {
  if (currentId) { const state = record(currentId); state.scroll = dom.conversation_scroll.scrollTop; if (bottom()) { state.unread = false; dom.conversation_latest.hidden = true; } }
 },{passive:true});
 function update(task) {
  const id = task?.id || '';
  if (id !== currentId) {
   if (currentId) { readComposer(); record(currentId).scroll = dom.conversation_scroll.scrollTop; }
   currentId = id; currentTask = task || null; dom.conversation_messages.replaceChildren();
   const state = id ? record(id) : null, draft = state?.draft || freshDraft();
   dom.message_text.value = draft.text; dom.message_kind.value = draft.kind; dom.message_recheck.checked = !!draft.recheck; resizeComposer();
   dom.conversation_latest.hidden = true; dom.conversation_error.hidden = true; dom.conversation_older.hidden = true;
   if (state) { render(state,{initial:!state.loaded}); dom.conversation_scroll.scrollTop = state.scroll; }
   else { dom.conversation_empty.hidden = false; dom.conversation_loading.hidden = true; }
  } else currentTask = task || null;
  renderComposer(); renderActivity(); renderEmpty();
  if (id) { const state = record(id); render(state); load(state); }
 }
 return {update,activate() { if (currentId) { const state = record(currentId); render(state); load(state,{force:true}); } resizeComposer(); },
  reset() { if (currentId) readComposer(); currentId = ''; currentTask = null; records.clear(); dom.conversation_messages.replaceChildren(); renderComposer(); renderActivity(); renderEmpty(); }};
}
