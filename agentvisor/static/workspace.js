import {t as txt,tr,getLanguage} from './i18n.js';
import {$,esc,icon,active,statuses,badge,toast} from './ui.js';

const complete = task => ['succeeded','completed_unverified'].includes(task?.status);
const shortStep = text => String(text).replace(/^\d+[.)]\s*/, '').split(/\s+[—–]\s+/)[0];

export function initWorkspace(ctx){
 let tab='plan';
 const sidebar=$('#task-sidebar'),toggle=$('#sidebar-toggle'),inspector=$('#task-inspector');
 function syncInspectorVisibility(){
  const desktop=matchMedia('(min-width:1180px)').matches;
  const visible=desktop?!document.body.classList.contains('inspector-hidden'):document.body.classList.contains('inspector-open');
  $('#inspector-toggle').setAttribute('aria-expanded',String(visible));
 }
 function closeSidebar(){
  document.body.classList.remove('sidebar-open');toggle.setAttribute('aria-expanded','false');
  $('#sidebar-backdrop').hidden=true;
 }
 function showInspector(name=tab){
  tab=name;
  document.body.classList.add('inspector-open');document.body.classList.remove('inspector-hidden');
  $('#inspector-toggle').setAttribute('aria-expanded','true');
  inspector.querySelectorAll('[data-inspector-tab]').forEach(el=>{
   const chosen=el.dataset.inspectorTab===name;el.setAttribute('aria-selected',String(chosen));el.tabIndex=chosen?0:-1;
  });
  $('#inspector-plan').hidden=name!=='plan';$('#inspector-details').hidden=name!=='details';
 }
 function openDocument(name){
  $('#document-title').textContent=name;
  $('#document-content').textContent=ctx.state.task?.documents?.[name]||txt('Документ появится после начала работы.');
  $('#document-dialog').showModal();
 }
 function renderRail(){
  const root=$('#workspace-task-list'),query=$('#task-search').value.trim().toLocaleLowerCase();
  const tasks=ctx.state.tasks.filter(t=>!query||`${t.name} ${t.workspace}`.toLocaleLowerCase().includes(query));
  const key=JSON.stringify([getLanguage(),ctx.state.selected,query,tasks.map(t=>[t.id,t.name,t.status,t.workspace])]);
  if(root.dataset.version===key)return;
  const focus=root.contains(document.activeElement)?document.activeElement?.dataset.workspaceTask:null;
  root.innerHTML=tasks.length?tasks.map(t=>`<button class="task-nav-item ${ctx.state.selected===t.id?'selected':''}" data-workspace-task="${esc(t.id)}" ${ctx.state.selected===t.id?'aria-current="true"':''} title="${esc(t.workspace)}"><span class="task-nav-name">${esc(t.name)}</span><span class="task-nav-meta"><span class="task-nav-state ${active.has(t.status)?'working':complete(t)?'complete':['blocked','failed'].includes(t.status)?'attention':''}"></span>${esc(statuses[t.status]||t.status)}<span class="task-nav-project">${esc(String(t.workspace).replace(/\\/g,'/').split('/').filter(Boolean).pop()||'')}</span></span></button>`).join(''):
   `<p class="sidebar-empty">${esc(txt(query?'Задачи не найдены':'Создайте задачу — она появится здесь.'))}</p>`;
  root.dataset.version=key;
  if(focus)[...root.querySelectorAll('[data-workspace-task]')].find(el=>el.dataset.workspaceTask===focus)?.focus({preventScroll:true});
 }
 function renderPlan(task){
  const items=task?.checklist||[],root=$('#checklist'),current=items.findIndex(s=>!s.done);
  const key=JSON.stringify([getLanguage(),task?.id,task?.status,items]);
  if(root.dataset.version===key)return;
  const open=new Set([...root.querySelectorAll('details[open]')].map(el=>el.dataset.stepId));
  const focused=root.contains(document.activeElement)?document.activeElement.closest('[data-step-id]')?.dataset.stepId:null;
  root.innerHTML=items.length?items.map((s,i)=>{
   const accepted=s.review_status==='accepted',pending=s.review_status==='pending';
   const label=accepted?txt('Проверено'):pending?txt('Ожидает проверки'):i===current&&active.has(task.status)?txt('В работе'):s.done?txt('Отмечено агентом'):txt('Предстоит выполнить');
   return `<details class="plan-step ${accepted?'done':''} ${i===current?'active':''}" data-step-id="${esc(s.id||String(i))}" ${open.has(s.id||String(i))?'open':''}><summary><span class="step-icon">${accepted?icon('check'):pending?icon('clock'):''}</span><span class="step-number">${String(i+1).padStart(2,'0')}</span><span class="step-text">${esc(shortStep(s.text))}</span><span class="step-status sr-only">${esc(label)}</span></summary><div class="step-evidence"><span class="badge ${accepted?'success':pending?'warning':'neutral'}">${esc(label)}</span><p>${esc(s.text)}</p></div></details>`;
  }).join(''):`<div class="empty small">${icon('list')}<strong>${esc(txt(task?'План ещё не составлен':'Выберите задачу'))}</strong><span>${esc(txt(task?'Агент составит проверяемые этапы после запуска.':'План и результаты выбранной задачи появятся здесь.'))}</span></div>`;
  root.dataset.version=key;
  if(focused)[...root.querySelectorAll('[data-step-id]')].find(el=>el.dataset.stepId===focused)?.querySelector('summary')?.focus({preventScroll:true});
  $('#current-step-name').textContent=current>=0?`${current+1}. ${shortStep(items[current].text)}`:items.length?txt('Все этапы отмечены'):txt('План появится после запуска');
  $('#show-current-step').hidden=current<0;
 }
 function render(task){
  renderRail();
  $('#task-status').innerHTML=task?badge(task.status):'';
  if(ctx.state.page==='overview'){
   $('#page-title').textContent=task?.name||txt(ctx.state.selected?'Загружаем задачу…':'Рабочая область');
   $('#page-subtitle').textContent=task?.workspace||txt('Выберите задачу или создайте новую');
  }
  const goal=$('#task-goal-content');if(goal.textContent!==task?.goal)goal.textContent=task?.goal||txt('Цель появится после создания задачи.');
  const docs=['GOAL.md','PROGRESS.md','MEMORY.md','DONE.md'];
  const key=JSON.stringify([task?.id,docs.map(name=>!!task?.documents?.[name])]);
  const links=$('#task-document-links');
  if(links.dataset.version!==key){
   links.innerHTML=docs.map(name=>`<button class="document-link" data-document="${name}" ${!task?.documents?.[name]?'disabled':''}>${icon('file')}<span>${name}</span>${icon('chevron')}</button>`).join('');links.dataset.version=key;
  }
 }
 toggle.addEventListener('click',()=>{
  if(document.body.classList.contains('sidebar-open'))closeSidebar();else{
   document.body.classList.add('sidebar-open');toggle.setAttribute('aria-expanded','true');$('#sidebar-backdrop').hidden=false;$('#task-search').focus();
  }
 });
 $('#sidebar-backdrop').addEventListener('click',closeSidebar);
 $('#inspector-toggle').addEventListener('click',()=>{
  const desktop=matchMedia('(min-width:1180px)').matches;
  const visible=desktop?!document.body.classList.contains('inspector-hidden'):document.body.classList.contains('inspector-open');
  if(visible){document.body.classList.remove('inspector-open');document.body.classList.add('inspector-hidden');$('#inspector-toggle').setAttribute('aria-expanded','false');}else showInspector();
 });
 inspector.addEventListener('click',event=>{const t=event.target.closest('[data-inspector-tab]');if(t)showInspector(t.dataset.inspectorTab);const doc=event.target.closest('[data-document]');if(doc)openDocument(doc.dataset.document);});
 inspector.addEventListener('keydown',event=>{
  if(!event.target.matches('[role="tab"]')||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();showInspector(event.key==='Home'?'plan':event.key==='End'?'details':tab==='plan'?'details':'plan');$(`#tab-${tab}`).focus();
 });
 $('#show-current-step').addEventListener('click',()=>{const el=$('#checklist .plan-step.active');if(el){el.open=true;el.scrollIntoView({block:'center',behavior:'instant'});el.querySelector('summary').focus({preventScroll:true});}});
 document.addEventListener('conversation-step',event=>{
  if(event.detail.taskId!==ctx.state.task?.id)return;
  showInspector('plan');const row=[...$('#checklist').querySelectorAll('[data-step-id]')].find(el=>el.dataset.stepId===event.detail.stepId);
  if(row){row.open=true;row.scrollIntoView({block:'center',behavior:'instant'});row.querySelector('summary').focus({preventScroll:true});}
 });
 $('#task-search').addEventListener('input',renderRail);
 $('#workspace-task-list').addEventListener('click',async event=>{
  const button=event.target.closest('[data-workspace-task]');if(!button)return;
  closeSidebar();try{await ctx.selectTask(button.dataset.workspaceTask);await ctx.navigate('overview');}catch(e){toast(e.message,true);}
 });
 document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&document.body.classList.contains('sidebar-open')){closeSidebar();toggle.focus();}
  else if(event.key==='Escape'&&document.body.classList.contains('inspector-open')&&!matchMedia('(min-width:1180px)').matches){document.body.classList.remove('inspector-open');$('#inspector-toggle').setAttribute('aria-expanded','false');$('#inspector-toggle').focus();}
 });
 window.addEventListener('resize',syncInspectorVisibility);
 syncInspectorVisibility();
 return {render,renderPlan,closeSidebar,showInspector,openDocument};
}
