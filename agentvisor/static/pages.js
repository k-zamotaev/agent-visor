import {t as txt,tr,markup,locale,getLanguage} from './i18n.js';
import {$,esc,number,gigabytes,duration,icon,toast,badge,eventRows} from './ui.js';
import {renderNetwork} from './network.js';
import {renderRuntime} from './runtime.js';
import {profileStrategy,profileAdvanced,readStrategy,bindStrategy,planText} from './profile.js';

let context;
const field=(id,label,value='',type='text',attrs='')=>`<div class="field"><label for="${id}">${label}</label><input id="${id}" type="${type}" value="${esc(value)}" ${attrs}></div>`;
const numeric=id=>Number($('#'+id).value);
async function busy(button,work){const label=button.innerHTML;button.disabled=true;button.textContent=txt('Выполняется…');try{await work();}catch(e){toast(e.message,true);}finally{button.disabled=false;button.innerHTML=label;}}
function taskList(){
 const target=$('#task-list');if(!target)return;
 target.innerHTML=context.state.tasks.length?context.state.tasks.map(t=>tr`<article class="task-row"><button class="task-title" data-select-task="${esc(t.id)}">${esc(t.name)}</button><p>${esc(t.workspace)}</p><div class="row-meta">${badge(t.status)}<small>${t.iteration} итераций · ${duration(t.elapsed)}</small></div></article>`).join(''):markup('<div class="empty small"><strong>Здесь будут ваши задачи</strong><span>Создайте первую задачу или запустите короткую демонстрацию.</span></div>');
}
function tasksPage(root){
 root.innerHTML=tr`<div class="task-layout"><form id="new-task" class="panel form-panel"><h2>Новая задача</h2><p>Агент выполняет по одному шагу и сохраняет результат в документах задачи.</p>
 ${field('new-name',txt('Название'),'','text',markup('required maxlength="120" placeholder="Например, тесты для API"'))}
 ${field('new-workspace',txt('Каталог проекта'),'','text',markup('required placeholder="D:\\project или /workspaces/project"'))}
 <div class="field"><label for="new-goal">Какой результат нужен?</label><textarea id="new-goal" rows="5" required minlength="3" maxlength="20000" placeholder="Опишите конечный результат и критерии готовности."></textarea><small>В Docker укажите путь внутри контейнера. Каталог должен существовать.</small></div>
 <div class="notice">Профиль: <strong>${esc(context.state.profile.model||txt('Автовыбор из установленных моделей'))}</strong> · ${number(context.state.profile.context)} токенов. <button type="button" class="text-button" data-page="models">Настроить</button></div>
 <div class="form-row">${field('new-iterations',txt('Максимум итераций'),40,'number','min="1" max="1000" required')}${field('new-hours',txt('Общий предел, часов'),12,'number','min="0.01" max="168" step="0.01" required')}</div>
 <details><summary>Параметры устойчивости и проверки</summary><div class="section-divider"></div>
 <div class="form-row">${field('new-timeout',txt('Таймаут итерации, минут'),30,'number','min="1" max="360" required')}${field('new-failures',txt('Ошибок подряд до остановки'),3,'number','min="1" max="10" required')}</div>
 <div class="form-row">${field('new-stall',txt('Итераций без нового шага'),5,'number','min="2" max="30" required')}${field('new-backoff',txt('Пауза после ошибки, секунд'),30,'number','min="1" max="300" required')}</div>
 <div class="field"><label for="new-verification">Команда независимой проверки</label><textarea id="new-verification" rows="2" placeholder='["python", "-m", "pytest", "-q"]'></textarea><small>JSON-массив: программа и её аргументы. Команда запускается в каталоге проекта без оболочки. Без неё результат получает статус «Завершена агентом».</small></div>
 <label class="check-label"><input id="new-autotune" type="checkbox" checked><span>Подбирать контекст при переполнении запроса или нехватке памяти</span></label></details>
 <label class="check-label"><input id="new-permissions" type="checkbox"><span>Разрешить OpenCode выполнять инструменты без вопросов.<small> Агент сможет изменять файлы и выполнять команды с правами пользователя. Используйте отдельный каталог проекта.</small></span></label>
 <div class="form-actions"><button class="button primary" type="submit" value="start">${icon('play')}Создать и запустить</button><button class="button" type="submit" value="save">Сохранить задачу</button></div></form>
 <aside><section class="panel"><div class="panel-heading"><h2>Ваши задачи</h2></div><div id="task-list"></div></section><section class="panel demo-panel"><h2>Попробовать без модели</h2><p class="prose">Четыре коротких шага в отдельном демонстрационном каталоге. Можно поставить на паузу и продолжить.</p><button class="button" id="demo-task">Запустить демонстрацию</button></section></aside></div>`;
 taskList();
 $('#demo-task').onclick=()=>context.startDemo();
 $('#new-task').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{
  let verification=[];try{verification=JSON.parse($('#new-verification').value.trim()||'[]');}catch{throw new Error(txt('Команда проверки должна быть JSON-массивом строк.'));}
  if(!Array.isArray(verification)||verification.some(v=>typeof v!=='string'||!v.length))throw new Error(txt('Укажите программу и аргументы массивом непустых строк.'));
  const t=await context.api('/tasks',{method:'POST',body:JSON.stringify({name:$('#new-name').value,workspace:$('#new-workspace').value,goal:$('#new-goal').value,profile:context.state.profile,language:getLanguage(),max_iterations:numeric('new-iterations'),max_hours:numeric('new-hours'),timeout_seconds:numeric('new-timeout')*60,max_failures:numeric('new-failures'),stall_limit:numeric('new-stall'),backoff_seconds:numeric('new-backoff'),verification,auto_permissions:$('#new-permissions').checked,auto_tune:$('#new-autotune').checked})});
  await context.selectTask(t.id);await context.navigate('overview');
  if(event.submitter.value==='start')await context.api(`/tasks/${t.id}/start`,{method:'POST'});
  await context.refresh();toast(event.submitter.value==='start'?txt('Задача запущена'):txt('Задача сохранена'));
 });};
}
function readProfile(){return {runtime:$('#profile-runtime').value,base_url:$('#profile-url').value,model:$('#profile-model').value.trim(),context:numeric('profile-context'),output_limit:numeric('profile-output'),manage_runtime:$('#profile-manage').checked,...readStrategy()};}
function inventoryPanel(){
 const info=context.state.modelInfo, node=$('#model-inventory');if(!node)return;
 if(!info){node.innerHTML=markup('<p class="quiet">Проверяем доступные модели…</p>');return;}
 node.innerHTML=`<div class="notice ${info.online?'':'warning'}">${esc(info.online?txt('Сервер модели отвечает.'):info.error||txt('Сервер не запущен. Нажмите «Запустить сервис» или запустите задачу с автоматическим управлением LM Studio.'))}</div>
 <div class="model-list">${(info.models||[]).map(m=>`<div class="model-option"><div><h3>${esc(m.name||m.id)}</h3><p>${m.size?gigabytes(m.size)+txt(' ГБ · '):''}${m.tool_use===false?txt('Инструменты не поддерживаются'):m.tool_use===true?txt('Поддерживает инструменты'):txt('Поддержка инструментов не подтверждена')}</p></div><button class="button" data-model="${esc(m.id)}">${m.loaded?txt('Выбрать · загружена'):txt('Выбрать')}</button></div>`).join('')||markup('<p class="prose">Установленных моделей пока нет. Для LM Studio скачайте модель через панель выше; для Ollama используйте ollama pull.</p>')}</div>`;
 $('#model-options').innerHTML=(info.models||[]).map(m=>`<option value="${esc(m.id)}"></option>`).join('');
 node.querySelectorAll('[data-model]').forEach(button=>button.onclick=()=>{$('#profile-model').value=button.dataset.model;$('#profile-form').dispatchEvent(new Event('change'));$('#profile-model').focus();});
 $('#profile-form').dispatchEvent(new Event('change'));
 if(info.benchmark&&$('#model-result').hidden)showModelResult(info.benchmark);
}
function showModelResult(result,apply=false){
 const output=$('#model-result');output.hidden=false;
 if(result.kind==='profile_plan'||result.plan){
  const plan={...(result.plan||result),load_config:result.load_config||result.loaded?.load_config};
  output.textContent=planText(result.request_seconds!==undefined?{...plan,samples:[result]}:plan);
  if(apply){$('#profile-context').value=plan.profile.context;$('#profile-output').value=plan.profile.output_limit;$('#profile-form').dispatchEvent(new Event('change'));}
  return;
 }
 if(result.request_seconds!==undefined)output.textContent=tr`Последний короткий замер\nМодель: ${result.profile.model}\nКонтекст: ${number(result.profile.context)}\nСкорость запроса: ${number(result.request_tps)} ток/с\nСкорость генерации: ${result.generation_tps?number(result.generation_tps)+txt(' ток/с'):txt('runtime не сообщает отдельно')}\nДлительность: ${result.request_seconds} с\n\n${result.note}`;
 else output.textContent=result.text||tr`Модель загружена\n${result.instance}\nКонтекст: ${number(result.context)} токенов`;
}
function modelsPage(root){
 const p=context.state.profile;
 root.innerHTML=tr`<section id="runtime-controls" class="panel network-panel"></section><div class="settings-grid"><form id="profile-form" class="panel form-panel"><h2>Профиль запуска</h2><p>Сохранённый профиль используется в новых задачах. Уже созданные задачи сохраняют свой профиль.</p>
 <div class="field"><label for="profile-runtime">Сервер модели</label><select id="profile-runtime"><option value="lmstudio" ${p.runtime==='lmstudio'?'selected':''}>LM Studio</option><option value="ollama" ${p.runtime==='ollama'?'selected':''}>Ollama</option></select></div>
 ${field('profile-url',txt('Адрес сервера'),p.base_url,'url','required')}<p class="prose">Локальный headless LM Studio: http://127.0.0.1:1234, в том числе внутри Docker. Для внешнего сервера укажите его корневой адрес без /v1.</p>
 <label class="check-label"><input id="profile-manage" type="checkbox" ${p.manage_runtime!==false?'checked':''}><span>Автоматически устанавливать и запускать локальный LM Studio при необходимости</span></label><hr class="section-divider">
 ${profileStrategy(p)}<hr class="section-divider">
 ${field('profile-model',txt('Модель'),p.model,'text',markup('list="model-options" placeholder="Пустое поле — автовыбор при запуске"'))}<datalist id="model-options"></datalist>
 <div class="form-row">${field('profile-context',txt('Контекст, токенов'),p.context,'number','min="4096" max="262144" step="1" required')}${field('profile-output',txt('Максимум ответа'),p.output_limit,'number','min="256" max="32768" required')}</div>
 <p class="prose">В автоматическом режиме значения уточняются при подборе. В ручном режиме лимит ответа должен быть меньше половины контекста.</p>
 ${profileAdvanced(p)}
 <div class="form-actions"><button class="button primary" type="submit">Сохранить профиль</button><button class="button" id="refresh-models" type="button">${icon('refresh')}Обновить список</button></div>
 <hr class="section-divider"><div class="model-actions"><button class="button" type="button" data-model-action="estimate">Оценить память</button><button class="button" type="button" data-model-action="load">Загрузить модель</button><button class="button" type="button" data-model-action="unload">Выгрузить из памяти</button><button class="button" type="button" data-model-action="benchmark">Измерить скорость</button></div>
 <p class="prose model-help">Загрузка и замер доступны, когда агент на паузе. Замер запускает короткий ответ модели; он не оценивает качество решения задач.</p><pre id="model-result" class="result-output" role="status" hidden></pre></form>
 <section class="panel"><div class="panel-heading"><h2>Доступно на узле</h2></div><div id="model-inventory"></div></section></div>`;
 $('#profile-runtime').onchange=()=>{$('#profile-url').value=$('#profile-runtime').value==='ollama'?'http://127.0.0.1:11434':'http://127.0.0.1:1234';$('#profile-model').value='';};
 $('#profile-form').onsubmit=e=>{e.preventDefault();busy(e.submitter,async()=>{context.state.profile=await context.api('/profile',{method:'PUT',body:JSON.stringify(readProfile())});toast(txt('Профиль сохранён для новых задач'));await context.refreshModels();inventoryPanel();});};
 $('#refresh-models').onclick=e=>busy(e.currentTarget,async()=>{await context.refreshModels();inventoryPanel();});
 root.querySelectorAll('[data-model-action]').forEach(button=>button.onclick=()=>busy(button,async()=>{if(!$('#profile-form').reportValidity())return;showModelResult({text:txt('Операция выполняется. Загрузка модели может занять несколько минут.')});const result=await context.api('/models/'+button.dataset.modelAction,{method:'POST',body:JSON.stringify(readProfile())});showModelResult(result,true);await context.refreshModels();}));
 renderRuntime($('#runtime-controls'),context,readProfile);
 bindStrategy(context);
 inventoryPanel();context.refreshModels().then(inventoryPanel);
}
function historyList(){
 const root=$('#history-list');if(!root)return;
 const search=$('#history-search').value.toLowerCase(),level=$('#history-level').value;
 const events=context.state.events;
 const rows=events.filter(e=>(!level||level===e.level)&&(!search||`${e.message} ${e.kind}`.toLowerCase().includes(search)));
 const version=`${getLanguage()}:${rows.at(-1)?.id}:${rows.length}:${search}:${level}:${context.state.focusEvent}`;
 if(root.dataset.version!==version&&!root.contains(document.activeElement)){
  root.innerHTML=rows.length?eventRows(rows,true,context.state.focusEvent):markup('<div class="empty small">Нет событий по выбранному фильтру.</div>');
  root.dataset.version=version;
 }
 $('#history-task').textContent=context.state.task?.name||txt('Выберите задачу на странице обзора');
}
function historyPage(root){
 root.innerHTML=tr`<section class="panel"><div class="panel-heading"><div><h2 id="history-task"></h2><p class="prose">Последние 200 событий выбранной задачи. Полный журнал хранится в локальной базе.</p></div></div><div class="toolbar"><input id="history-search" type="search" aria-label="Поиск по событиям" placeholder="Поиск по событиям"><select id="history-level" aria-label="Уровень событий"><option value="">Все события</option><option value="info">Информация</option><option value="warning">Предупреждения</option><option value="error">Ошибки</option></select><button class="button" id="export-events">${icon('download')}Скачать JSON</button></div><div id="history-list" class="history-events"></div></section>`;
 $('#history-search').oninput=historyList;$('#history-level').onchange=historyList;
 $('#export-events').onclick=()=>{const blob=new Blob([JSON.stringify({task:context.state.task?.name,events:context.state.events},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='agentvisor-events.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);};historyList();
 if(context.state.focusEvent!==undefined){const selected=document.getElementById('event-'+context.state.focusEvent);selected?.scrollIntoView({block:'center'});selected?.focus({preventScroll:true});}
}
async function settingsPage(root){
 const m=context.state.system||{},p=context.state.profile;
 const row=(key,value)=>`<div><dt>${key}</dt><dd>${esc(value)}</dd></div>`;
 root.innerHTML=tr`<section id="network-settings" class="panel network-panel"></section><div class="settings-grid"><section class="panel"><h2>Этот узел</h2><dl class="definition">${row(txt('Платформа'),(m.os||'—')+(m.container?' · Docker':''))}${row(txt('Процессор'),m.cpu||'—')}${row(txt('Потоки CPU'),m.cpu_count||'—')}${row(txt('Оперативная память'),gigabytes(m.ram_total)+txt(' ГБ'))}${row(txt('Видеокарта'),m.gpus?.map(g=>g.name).join(', ')||txt('NVIDIA GPU не обнаружен'))}${row('OpenCode',m.opencode||txt('Не найден в PATH'))}${row('LM Studio CLI',m.lms||txt('Не найден'))}${row(txt('Данные'),context.state.dataDirectory||'.agentvisor-data')}</dl></section>
 <section class="panel"><h2>Как устроен запуск</h2><div class="prose model-help"><p>Одновременно работает одна задача. Каждая итерация — новая сессия OpenCode; цель, план и результаты остаются в каталоге проекта.</p><p>После перезапуска AgentVisor незаконченная задача остаётся на паузе. Продолжить её можно из обзора.</p><p>Смена цели применяется со следующей итерации. Пауза завершает текущее дерево процессов и сохраняет уже записанные файлы.</p><p>Рекомендации по памяти не гарантируют наилучшую скорость. Измерьте выбранный профиль перед длительным запуском.</p></div><hr class="section-divider"><h3>Подключение к модели</h3><p class="prose model-help">${esc(p.runtime==='ollama'?'Ollama':'LM Studio')} · ${esc(p.base_url)}</p><button class="button" data-page="models">Изменить профиль</button></section></div>`;
 await renderNetwork($('#network-settings'),context);
}
export async function renderPage(page,ctx){
 context=ctx;const root=$('#page-'+page);
 await ({tasks:tasksPage,models:modelsPage,history:historyPage,settings:settingsPage})[page](root);
}
document.addEventListener('tasks-update',taskList);
document.addEventListener('model-inventory-update',inventoryPanel);
document.addEventListener('history-update',historyList);
document.addEventListener('click',e=>{const button=e.target.closest('[data-select-task]');if(button&&context)context.selectTask(button.dataset.selectTask).then(()=>context.navigate('overview')).catch(err=>toast(err.message,true));});
