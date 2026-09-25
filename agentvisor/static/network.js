import {t as txt,tr,markup} from './i18n.js';
import {$,esc,toast} from './ui.js';

export async function renderNetwork(root,context){
 root.innerHTML=markup('<h2>Сетевой доступ</h2><p class="prose">Загрузка настроек…</p>');
 let data;
 try{data=await context.api('/network');}
 catch(error){root.innerHTML=`<h2>${txt('Сетевой доступ')}</h2><p class="form-error" role="alert">${esc(error.message)}</p>`;return;}
 if(!root.isConnected)return;
 const remote=!['localhost','127.0.0.1','[::1]'].includes(location.hostname);
 root.innerHTML=tr`<div class="panel-heading"><div><h2>Сетевой доступ</h2><p class="prose">Выберите, с каких устройств можно открывать панель.</p></div><span class="badge neutral">${esc(data.active_host)}</span></div>
 <form id="network-form"><div class="form-row"><div><label for="network-host">Адрес прослушивания</label><select id="network-host" ${data.bind_override?'disabled':''}><option value="127.0.0.1">127.0.0.1 — только этот компьютер</option><option value="0.0.0.0">0.0.0.0 — все сетевые интерфейсы</option></select></div>
 <div><label for="network-code">Код доступа для других устройств</label><div class="code-field"><input id="network-code" type="password" readonly value="${esc(data.access_code)}" autocomplete="off"><button class="button" type="button" id="show-network-code">Показать</button></div></div></div>
 <p class="prose">С другого устройства откройте адрес этого компьютера и введите код. 0.0.0.0 — режим прослушивания, а не адрес для браузера.</p>
 <p class="prose">Разрешите входящие подключения к порту панели в брандмауэре только для доверенной локальной сети.</p>
 ${data.bind_override?markup('<p class="notice">Адрес внутри контейнера задан при запуске. Доступ с хоста настраивается через AGENTVISOR_BIND_ADDRESS в .env и пересоздание контейнера Docker Compose.</p>'):''}
 <div id="network-addresses"><p class="field-label">Адреса этого компьютера</p>${data.addresses.length?`<ul class="network-addresses">${data.addresses.map(url=>`<li><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a></li>`).join('')}</ul>`:markup('<p class="quiet">Сетевой адрес не обнаружен.</p>')}</div>
 <p id="network-pending" class="notice" hidden>Настройки сохранены. Перезапустите панель, чтобы применить новый адрес.</p>
 <p id="network-local-warning" class="notice" hidden>После переключения на 127.0.0.1 панель будет доступна только на основном компьютере.</p>
 <p id="network-error" class="form-error" role="alert" hidden></p>
 <div class="form-actions"><button class="button" type="submit" ${data.bind_override?'disabled':''}>Сохранить</button><button class="button primary" type="button" id="restart-panel">Применить и перезапустить</button>${data.authentication_required?markup('<button class="text-button" type="button" id="network-logout">Выйти</button>'):''}</div>
 <p class="prose model-help">Для перезапуска сначала поставьте активную задачу на паузу. Настройки и история сохранятся.</p>
 ${!data.managed?markup('<p class="notice">Для применения настроек перезапустите AgentVisor через скрипт запуска.</p>'):''}</form>`;
 const host=$('#network-host'),pending=$('#network-pending'),restart=$('#restart-panel');
 host.value=data.host;
 function update(){
  pending.hidden=!data.restart_required;
  $('#network-addresses').hidden=data.bind_override||host.value!=='0.0.0.0';
  $('#network-local-warning').hidden=!remote||host.value!=='127.0.0.1';
  restart.disabled=!data.managed||data.bind_override||host.value===data.active_host;
 }
 host.onchange=update;update();
 $('#show-network-code').onclick=()=>{const input=$('#network-code'),show=input.type==='password';input.type=show?'text':'password';$('#show-network-code').textContent=show?txt('Скрыть'):txt('Показать');};
 async function save(apply){
  const error=$('#network-error'),buttons=[...root.querySelectorAll('button')];
  error.hidden=true;host.disabled=true;buttons.forEach(button=>button.disabled=true);
  try{
   data=await context.api('/network',{method:'PUT',body:JSON.stringify({host:host.value})});
   pending.hidden=!data.restart_required;
   if(!apply){toast(txt('Настройки сохранены'));return;}
   context.state.reconnecting=true;
   await context.api('/restart',{method:'POST'});
   if(remote&&host.value==='127.0.0.1'){
    root.innerHTML=markup('<h2>Сетевой доступ отключается</h2><p class="prose">Откройте панель на основном компьютере. История и настройки сохранены.</p>');
    return;
   }
   pending.hidden=false;pending.textContent=txt('Панель перезапускается. Подключаемся снова…');
   const deadline=Date.now()+30000;
   while(Date.now()<deadline){
    await new Promise(resolve=>setTimeout(resolve,700));
    try{
     const response=await fetch('/api/health',{cache:'no-store',signal:AbortSignal.timeout(1500)});
     if(response.ok&&(await response.json()).bind_host===host.value){location.reload();return;}
    }catch{}
   }
   throw new Error(txt('Панель ещё не ответила. Обновите страницу или проверьте окно запуска.'));
  }catch(failure){context.state.reconnecting=false;error.textContent=failure.message;error.hidden=false;}
  finally{if(root.isConnected&&host.isConnected){buttons.forEach(button=>button.disabled=false);host.disabled=data.bind_override;update();}}
 }
 $('#network-form').onsubmit=event=>{event.preventDefault();save(false);};
 restart.onclick=()=>save(true);
 $('#network-logout')?.addEventListener('click',async()=>{try{await context.api('/auth/logout',{method:'POST'});location.reload();}catch(error){toast(error.message,true);}});
}
