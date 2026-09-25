import {t as txt,tr,markup} from './i18n.js';
import {number,gigabytes,toast} from './ui.js';

export function renderRuntime(root,context,readProfile){
 root.innerHTML=markup(`<div class="panel-heading"><div><h2>Сервис модели</h2><p class="prose">AgentVisor подготовит и запустит headless LM Studio перед задачей. Открывать приложение LM Studio не требуется.</p></div><span id="runtime-kind" class="badge neutral">Проверка…</span></div>
 <p id="runtime-stage" class="prose" role="status"></p><p id="runtime-error" class="form-error" role="alert" hidden></p>
 <div class="form-actions"><button type="button" class="button primary" id="runtime-start">Запустить сервис</button><button type="button" class="button" id="runtime-stop" disabled>Остановить свой сервис</button></div>
 <p class="prose model-help">Если CLI отсутствует, AgentVisor установит llmster с lmstudio.ai в каталог пользователя. Существующий сервер используется повторно. Приложение LM Studio и чужие модели автоматически не закрываются.</p>
 <details class="runtime-download"><summary>Скачать новую модель</summary><form id="download-form"><div class="field"><label for="download-model">Модель из каталога или ссылка Hugging Face</label><input id="download-model" required minlength="3" maxlength="500" placeholder="Например, qwen/qwen3-8b"></div>
 <div class="field"><label for="download-quantization">Квантизация для Hugging Face (необязательно)</label><input id="download-quantization" maxlength="40" pattern="[A-Za-z0-9_.-]*" placeholder="Q4_K_M"></div>
 <p class="prose">Вес модели может занимать несколько гигабайт. После скачивания она появится в списке; загрузка в память выполняется отдельно.</p>
 <button class="button" type="submit" id="download-start">Скачать модель</button></form></details>
 <div id="download-progress" class="notice" role="status" hidden></div>`);
 const find=selector=>root.querySelector(selector);
 let pending=false,refreshing=false,service=null,lastDownload=null;
 const labels={'not-installed':'Не установлен','not-running':'Остановлен',headless:'Headless · llmster',desktop:'LM Studio · приложение',remote:'Внешний сервер',error:'Требует внимания'};
 function controls(){
  const p=readProfile();let local=false;
  try{local=['localhost','127.0.0.1','[::1]'].includes(new URL(p.base_url).hostname);}catch{}
  const manageable=p.runtime==='lmstudio'&&local;
  find('#runtime-start').disabled=pending||!manageable;
  find('#runtime-stop').disabled=pending||!manageable||!service?.owned;
  find('#download-start').disabled=pending||p.runtime!=='lmstudio'||['downloading','paused'].includes(lastDownload?.status);
 }
 async function refresh(){
  if(refreshing||!root.isConnected||root.closest('.page')?.hidden)return;
  refreshing=true;
  try{
   service=await context.api('/runtime');
   if(!root.isConnected)return;
   find('#runtime-kind').textContent=txt(labels[service.kind]||service.kind);
   find('#runtime-stage').textContent=service.stage||txt(service.kind==='remote'?'Запуск удалённого сервера выполняется на его узле.':'Сервис будет запущен при необходимости.');
   if(service.error&&!pending){find('#runtime-error').textContent=service.error;find('#runtime-error').hidden=false;}
   const job=await context.api('/download');
   if(!root.isConnected)return;
   if(job){
    const status={downloading:'Скачивание',paused:'Скачивание приостановлено',completed:'Скачивание завершено',failed:'Ошибка скачивания'};
    find('#download-progress').hidden=false;
    find('#download-progress').textContent=`${txt(status[job.status]||job.status)}: ${job.model} · ${gigabytes(job.downloaded_bytes||0)} / ${job.total_size_bytes?gigabytes(job.total_size_bytes):'—'} ${txt('ГБ')}${job.bytes_per_second?tr` · ${number(job.bytes_per_second/1024**2)} МБ/с`:''}`;
    if(job.status==='completed'&&lastDownload?.status!=='completed'){await context.refreshModels();document.dispatchEvent(new Event('model-inventory-update'));}
   }
   lastDownload=job;controls();
  }catch(error){if(root.isConnected&&!pending){find('#runtime-error').textContent=error.message;find('#runtime-error').hidden=false;}}
  finally{refreshing=false;}
 }
 async function run(work){
  pending=true;controls();find('#runtime-error').hidden=true;
  find('#runtime-stage').textContent=txt('Подготовка сервиса. Первый запуск может занять несколько минут.');
  try{const result=await work();if(result.text)toast(result.text);await context.refreshModels();document.dispatchEvent(new Event('model-inventory-update'));}
  catch(error){if(root.isConnected){find('#runtime-error').textContent=error.message;find('#runtime-error').hidden=false;}}
  finally{pending=false;await refresh();if(root.isConnected)controls();}
 }
 find('#runtime-start').onclick=()=>run(()=>context.api('/models/start_service',{method:'POST',body:JSON.stringify(readProfile())}));
 find('#runtime-stop').onclick=()=>run(()=>context.api('/models/stop_service',{method:'POST',body:JSON.stringify(readProfile())}));
 find('#download-form').onsubmit=event=>{event.preventDefault();run(()=>context.api('/download',{method:'POST',body:JSON.stringify({profile:readProfile(),model:find('#download-model').value.trim(),quantization:find('#download-quantization').value.trim()})}));};
 root.closest('.page').querySelector('#profile-form').addEventListener('change',controls);
 controls();refresh();
 const timer=setInterval(()=>{if(!root.isConnected||root.closest('.page')?.hidden){clearInterval(timer);return;}refresh();},2500);
}
