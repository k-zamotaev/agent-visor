import {t as txt,tr,markup,locale,getLanguage,captureStatic,setLanguage} from './i18n.js';
import {$,esc,number,gigabytes,time,duration,icon,icons,toast,statuses,active,badge,eventRows,updateReasoning,chart} from './ui.js';
import {bindLimits,renderLimits,openLimits,budgetExhausted} from './limits.js';
import {initConversation} from './conversation.js';
import {initWorkspace} from './workspace.js';

const state={token:'',profile:{},tasks:[],task:null,events:[],system:null,modelInfo:null,page:'overview',connected:false,
 selected:localStorage.getItem('agentvisor-task') || '',refreshing:false,reconnecting:false};
let workspace,conversation;
captureStatic();
$('#language-picker').value=getLanguage();
let pollCount=0;
$('#chart-metric').value=localStorage.getItem('agentvisor-chart-metric')==='generation'?'generation':'session';

async function api(path,options={}) {
 const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json','X-AgentVisor-Token':state.token,'Accept-Language':getLanguage(),...options.headers}});
 const body=await response.json();
 if(!response.ok){
  if(response.status===401&&path!=='/auth/login')showLogin();
  const error=new Error(typeof body.detail==='string'?body.detail:body.detail?.map?.(e=>e.msg).join('; ') || txt('Не удалось выполнить запрос'));
  error.status=response.status;throw error;
 }
 return body;
}
function setText(id,value){const el=$(id);if(el && el.textContent!==String(value))el.textContent=value;}
function showLogin(){state.token='';$('#login-language').value=getLanguage();if(!$('#login-dialog').open)$('#login-dialog').showModal();}
async function selectTask(id){
 if(state.selected!==id){state.selected=id;state.task=null;state.events=[];conversation?.update(null);localStorage.setItem('agentvisor-task',id);renderOverview();}
 if(state.refreshing)await state.refreshing;
 return refresh();
}
function renderOverview(){
 const t=state.task, events=state.events, machine=state.system;
 renderLimits(t);
 const items=t?.checklist || [], done=items.filter(x=>x.done).length;
 setText('#metric-progress',items.length?`${done} / ${items.length}`:'—');
 const pending=items.filter(x=>x.review_status==='pending').length;
 setText('#progress-caption',pending?tr`Ожидают проверки: ${pending}`:items.length?txt('Отмечено агентом в плане'):txt('План появится после запуска'));
 setText('#metric-time',t?duration(t.elapsed):'—');
 setText('#time-caption',t?tr`Итерация ${String(t.iteration).padStart(2,'0')} из ${t.max_iterations}`:txt('Ожидание первой задачи'));
 const metricEvents=t?.metrics||events;
 const completed=metricEvents.filter(e=>e.kind==='iteration_finished'&&e.data?.duration>0);
 const tokens=t?.output_tokens||0,seconds=t?.agent_seconds||0;
 setText('#metric-speed',tokens&&seconds?tr`${number(tokens/seconds)} ток/с`:'—');
 setText('#speed-caption',t?.mode==='demo'?txt('Симуляция · модель не используется'):txt('С учётом инструментов и ожидания'));
 const generation=t?.generation_sample;
 const generating=t?.status==='running'&&t?.generation_activity?.active&&Date.now()/1000-t.generation_activity.time<5;
 setText('#metric-generation',generation?tr`${number(generation.tokens_per_second)} ток/с`:'—');
 setText('#generation-caption',generation?tr`Последний ответ · ${time(generation.time)}`:txt('Ожидание ответа модели'));
 if(generating)setText('#generation-caption',generation?txt('Идёт генерация · показан последний ответ'):txt('Идёт генерация · замер после ответа'));
 setText('#metric-recovery',t?number(t.recoveries):'—');
 setText('#recovery-caption',t?.recoveries?txt('Повторные попытки продолжения'):txt('История повторных запусков'));
 setText('#plan-label',items.length?tr`${done} из ${items.length} отмечено`:txt('Пока нет шагов'));
 $('#plan-progress').value=items.length?done/items.length*100:0;
 const current=items.findIndex(x=>!x.done);
 workspace?.renderPlan(t);
 const generationChart=$('#chart-metric').value==='generation';
 const samples=generationChart?metricEvents.filter(e=>e.kind==='generation_sample'&&e.data.tokens_per_second>0).map(e=>({time:e.time,label:time(e.time),rate:e.data.tokens_per_second})):
  completed.filter(e=>e.data.output_tokens>0).map(e=>({time:e.time,label:`№${e.data.iteration}`,rate:e.data.output_tokens/e.data.duration}));
 const recoveries=metricEvents.filter(e=>e.kind==='recovering').map(e=>{const next=samples.findIndex(s=>s.time>=e.time);return {index:next<0?samples.length-1:next,label:txt('Повтор · ')+time(e.time).slice(0,5),title:e.message};});
 $('#speed-chart').innerHTML=chart(samples,recoveries,$('#speed-chart').clientWidth,generationChart);
 setText('#chart-note',generationChart?txt('Скорость ответа по потоку токенов · без ожидания первого токена и работы инструментов'):txt('Выходные токены / длительность завершённой итерации'));
 const p=t?.resolved_profile || t?.profile || state.profile;
 setText('#model-name',p.model || txt('Выберите модель'));
 setText('#model-runtime',`${p.runtime==='ollama'?'Ollama':'LM Studio'}${p.context?tr` · ${number(p.context)} токенов`:''}`);
 setText('#context-status',t?.documents?.['GOAL.md']&&t?.documents?.['PROGRESS.md']?txt('Контекст задачи сохранён'):txt('Контекст ещё не создан'));
 const health=t?.runtime_health;
 const watched=t?.status==='running'&&p.watchdog!==false&&health&&Date.now()/1000-health.checked_at<20;
 const isLoaded=watched?health.ok:state.modelInfo?.models?.some(m=>m.id===p.model&&m.loaded);
 const demoRun=t?.mode==='demo';
 $('#model-badge').className='badge '+(isLoaded&&!demoRun?'success':'neutral');
 setText('#model-badge',demoRun?txt('Без модели'):isLoaded?txt('Загружена'):txt('Не загружена'));
 const healthNode=$('#runtime-health');
 if(healthNode){
  healthNode.hidden=demoRun||!t;
  healthNode.className='runtime-health '+(watched&&!health.ok?'warning':'');
  healthNode.textContent=p.watchdog===false?txt('Наблюдение за моделью выключено'):watched?
   (health.ok?tr`API модели доступен · проверено ${time(health.checked_at)}`:txt(health.message)):
   active.has(t?.status)?txt('Наблюдение включится после подготовки модели'):txt('Наблюдение приостановлено вместе с задачей');
 }
 if(machine){
  const gpu=machine.gpus?.[0];
  setText('#gpu-name',gpu?tr`${gpu.name} · ${gigabytes(gpu.total)} ГБ`:txt('GPU не обнаружен · доступен CPU'));
  $('#vram-meter').value=gpu?(gpu.total-gpu.free)/gpu.total*100:0;
  setText('#vram-value',gpu?tr`${gigabytes(gpu.total-gpu.free)} / ${gigabytes(gpu.total)} ГБ`:txt('Нет данных'));
  $('#ram-meter').value=machine.ram_total?machine.ram_used/machine.ram_total*100:0;
  setText('#ram-value',tr`${gigabytes(machine.ram_used)} / ${gigabytes(machine.ram_total)} ГБ`);
 }
 $('#edit-goal').disabled=!t||['succeeded','completed_unverified'].includes(t.status);
 $('#stop-control').hidden=!t;
 $('#stop-control').disabled=!t||!active.has(t.status);
 const primary=$('#primary-control');
 primary.disabled=!!t&&['pausing','stopping'].includes(t.status);
 primary.innerHTML=!t?tr`${icon('plus')}<span>Новая задача</span>`:active.has(t.status)?tr`${icon('pause')}<span>Пауза</span>`:['succeeded','completed_unverified'].includes(t.status)?tr`${icon('plus')}<span>Новая задача</span>`:budgetExhausted(t)?tr`${icon('edit')}<span>Изменить лимиты</span>`:`${icon('play')}<span>${t.status==='draft'?txt('Запустить'):txt('Продолжить')}</span>`;
 $('#open-progress').disabled=!t;
 $('#run-notice').hidden=!t||(!t.reason&&t.mode!=='demo');
 $('#run-notice').className='notice '+(['blocked','failed'].includes(t?.status)?'warning':'');
 setText('#run-notice',t?`${t.mode==='demo'?txt('ДЕМО · симуляция supervisor, модель и ваш проект не используются. '):''}${statuses[t.status] || t.status}${t.reason?' — '+t.reason:''}${t.goal_version!==t.applied_goal_version&&t.iteration>0?txt(' · Новая цель ожидает следующей итерации.'):''}`:'');
 workspace?.render(t);
 // A hidden scroll container has zero geometry. Rendering it would mistake a
 // reader's position for the bottom and discard it while another page is open.
 if(state.page==='overview')conversation?.update(t);
 setText('#mode-label',t?.mode==='demo'?txt('ДЕМО · симуляция запуска'):txt('Локальное управление'));
}
async function refreshModels(){const language=getLanguage();try{const info=await api('/models');if(language!==getLanguage())return;state.modelInfo=info;renderOverview();}catch(error){if(language===getLanguage())state.modelInfo={online:false,models:[],error:error.message};}}
async function refresh(){
 if(!state.token||state.reconnecting)return;
 if(state.refreshing)return state.refreshing;
 const language=getLanguage();
 state.refreshing=(async()=>{
 try{
  const [tasks,system]=await Promise.all([api('/tasks'),api('/system')]);
  if(language!==getLanguage())return;
  state.tasks=tasks;state.system=system;
  if(!tasks.some(t=>t.id===state.selected))state.selected=tasks[0]?.id || '';
  const selected=state.selected;
  if(selected){
   const [task,events]=await Promise.all([api(`/tasks/${selected}`),api(`/tasks/${selected}/events?grouped=true`)]);
   if(language!==getLanguage()||selected!==state.selected)return;
   state.task=task;state.events=events;
  }else{state.task=null;state.events=[];}
  state.connected=true;$('#connection-error').hidden=true;$('#connection-dot').classList.remove('off');
  setText('#connection-label',txt('Локальный узел'));setText('#node-os',tr`${system.os} · подключён`);setText('#task-count',tasks.length||'');
  setText('#last-updated',tr`Обновлено ${new Date().toLocaleTimeString(locale(),{hour12:false})}`);
  renderOverview();
  if(state.page==='history')document.dispatchEvent(new CustomEvent('history-update'));
  if(state.page==='tasks')document.dispatchEvent(new CustomEvent('tasks-update'));
 }catch(error){if(language!==getLanguage())return;state.connected=false;$('#connection-error').hidden=false;setText('#connection-error',txt('Связь с AgentVisor потеряна. Данные могут быть устаревшими. ')+error.message);$('#connection-dot').classList.add('off');setText('#connection-label',txt('Нет связи'));}
 finally{state.refreshing=false;}
 })();
 return state.refreshing;
}
const names={overview:['Рабочая область','Выберите задачу или создайте новую','Рабочая область'],tasks:['Задачи','Одна цель. Проверяемые шаги. Сохранённый контекст.','Задачи'],models:['Модели и оборудование','Локальная модель и профиль под вашу машину','Модели'],history:['История выполнения','Причины остановок, ответы агента и результаты проверок','История'],settings:['Настройки','Подключение к модели и состояние установки','Настройки']};
async function navigate(page){
 if(!names[page])page='overview';
 state.page=page;document.body.dataset.page=page;location.hash=page;workspace?.closeSidebar();
 document.querySelectorAll('.page').forEach(el=>{el.hidden=el.id!==`page-${page}`;});
 document.querySelectorAll('.nav-item').forEach(el=>{const current=el.dataset.page===page;el.classList.toggle('active',current);if(current)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
 setText('#page-title',txt(names[page][0]));setText('#page-subtitle',txt(names[page][1]));setText('#breadcrumb',txt(names[page][2]));
 $('#overview-actions').hidden=page!=='overview';
 if(page==='overview'){renderOverview();conversation?.activate();}else{
  const {renderPage}=await import('./pages.js');
  await renderPage(page,{state,api,refresh,refreshModels,navigate,selectTask,startDemo});
 }
}
async function control(action){
 if(!state.task)return;
 try{await api(`/tasks/${state.task.id}/${action}`,{method:'POST'});await refresh();toast(action==='pause'?txt('Запрошена пауза'):action==='stop'?txt('Запрошена остановка'):txt('Запуск принят'));}
 catch(error){toast(error.message,true);}
}
async function startDemo(){
 try{const t=await api('/tasks',{method:'POST',body:JSON.stringify({name:txt('Демонстрация AgentVisor'),workspace:'.',goal:txt('Показать передачу состояния между короткими сессиями.'),mode:'demo',profile:state.profile,language:getLanguage()})});await api(`/tasks/${t.id}/start`,{method:'POST'});await selectTask(t.id);await navigate('overview');}
 catch(error){toast(error.message,true);}
}
document.addEventListener('click',event=>{
 const journal=event.target.closest('[data-event-id]');if(journal){state.focusEvent=journal.dataset.eventId;navigate('history').catch(e=>toast(e.message,true));}
 const nav=event.target.closest('button[data-page],a[data-page]');if(nav)navigate(nav.dataset.page).catch(e=>toast(e.message,true));
 if(event.target.closest('[data-close]'))event.target.closest('dialog').close();
 if(event.target.closest('#quick-demo'))startDemo();
});
document.addEventListener('conversation-event',async event=>{
 const {taskId,eventId}=event.detail||{};
 if(taskId!==state.task?.id||!Number.isInteger(Number(eventId)))return;
 try{
  const entry=await api(`/tasks/${taskId}/events/${Number(eventId)}`);
  if(taskId!==state.task?.id)return;
  $('#document-title').textContent=txt('Запись журнала')+' · '+eventId;
  $('#document-content').textContent=(entry.truncated?txt('Показана часть длинной записи.')+'\n\n':'')+
   entry.message+(entry.kind==='tool'?'\n\n'+JSON.stringify(entry.data,null,2):'');
  $('#document-dialog').showModal();
 }catch(error){toast(error.message,true);}
});
$('#chart-metric').addEventListener('change',e=>{localStorage.setItem('agentvisor-chart-metric',e.target.value);renderOverview();});
$('#language-picker').addEventListener('change',async event=>{
 const picker=event.target,page=state.page,selected=state.selected;
 // The conversation owns task-scoped drafts and keyed disclosures. A snapshot
 // taken before asynchronous refresh must never overwrite another task's draft.
 const values=[...document.querySelectorAll('.page input,.page textarea,.page select')].filter(el=>!el.closest('#message-form')).map(el=>({id:el.id,value:el.value,checked:el.checked}));
 const legacyDetails=()=>[...document.querySelectorAll('.page details')].filter(el=>!el.closest('#conversation-messages,#checklist,#message-form'));
 const disclosures=legacyDetails().map(el=>el.open);
 const pending=state.refreshing;
 picker.disabled=true;
 try{
  setLanguage(picker.value);
  await pending;await Promise.all([refresh(),refreshModels()]);
  if(state.page!==page)return;
  await navigate(page);
  if(state.selected!==selected)return;
  for(const value of values){const el=document.getElementById(value.id);if(el){el.value=value.value;if('checked' in el)el.checked=value.checked;}}
  if(page==='settings')$('#network-host')?.dispatchEvent(new Event('change'));
  if(page==='models')$('#profile-form')?.dispatchEvent(new Event('change'));
  legacyDetails().forEach((el,i)=>{if(disclosures[i]!==undefined)el.open=disclosures[i];});
  if(page==='history'){if($('#history-list')?.contains(document.activeElement))document.activeElement.blur();document.dispatchEvent(new CustomEvent('history-update'));}
 }catch(error){toast(error.message,true);}finally{picker.disabled=false;picker.focus();}
});
bindLimits({state,api,refresh});
$('#primary-control').addEventListener('click',()=>!state.task||['succeeded','completed_unverified'].includes(state.task.status)?navigate('tasks'):active.has(state.task.status)?control('pause'):budgetExhausted(state.task)?(workspace.showInspector('details'),openLimits()):control('start'));
$('#stop-control').addEventListener('click',()=>control('stop'));
$('#edit-goal').addEventListener('click',()=>{if(!state.task)return;$('#goal-text').value=state.task.goal;$('#edit-max').value=state.task.max_iterations;$('#edit-timeout').value=Math.round(state.task.timeout_seconds/60);$('#goal-dialog').showModal();});
$('#goal-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await api(`/tasks/${state.task.id}`,{method:'PATCH',body:JSON.stringify({goal:$('#goal-text').value,max_iterations:Number($('#edit-max').value),timeout_seconds:Number($('#edit-timeout').value)*60})});$('#goal-dialog').close();toast(txt('Изменения сохранены'));await refresh();}catch(e){toast(e.message,true);}finally{button.disabled=false;}});
$('#open-progress').addEventListener('click',()=>workspace.openDocument('PROGRESS.md'));
window.addEventListener('hashchange',()=>{const p=location.hash.slice(1)||'overview';if(p!==state.page)navigate(p).catch(e=>toast(e.message,true));});
window.addEventListener('resize',()=>{if(state.page==='overview')renderOverview();});
workspace=initWorkspace({state,api,refresh,navigate,selectTask});
conversation=initConversation({state,api,refresh,navigate,control});
icons();
async function connect(){
 const session=await api('/session');state.token=session.token;state.profile=session.profile;state.dataDirectory=session.data_directory;setText('#app-version',session.version);
 await refresh();refreshModels();
 await navigate(location.hash.slice(1)||'overview');
}
$('#login-dialog').addEventListener('cancel',event=>event.preventDefault());
$('#login-language').addEventListener('change',event=>{setLanguage(event.target.value);$('#language-picker').value=getLanguage();$('#login-error').hidden=true;});
$('#login-form').addEventListener('submit',async event=>{
 event.preventDefault();const button=event.submitter,error=$('#login-error');button.disabled=true;error.hidden=true;
 try{await api('/auth/login',{method:'POST',body:JSON.stringify({code:$('#login-code').value})});$('#login-code').value='';$('#login-dialog').close();await connect();}
 catch(failure){error.textContent=failure.message;error.hidden=false;}
 finally{button.disabled=false;}
});
try{await connect();}catch(error){if(error.status!==401)toast(txt('Не удалось подключиться: ')+error.message,true);}
setInterval(()=>{if(!document.hidden&&state.token&&!state.reconnecting){refresh();if(++pollCount%15===0)refreshModels();}},2000);
