import {t as txt,tr,markup} from './i18n.js';
import {esc,number,gigabytes} from './ui.js';

const select=(id,label,options,value)=>`<div class="field"><label for="${id}">${esc(txt(label))}</label><select id="${id}">${options.map(([key,text])=>`<option value="${key}" ${value===key?'selected':''}>${esc(txt(text))}</option>`).join('')}</select></div>`;
const input=(id,label,value,limits)=>`<div class="field"><label for="${id}">${esc(txt(label))}</label><input id="${id}" type="number" value="${value??''}" ${limits}></div>`;
const reasoningLabels={off:'Выключено',on:'Включено',low:'Низкое',medium:'Среднее',high:'Высокое',xhigh:'Максимальное'};

export function profileStrategy(p){
 return markup(`<div class="profile-strategy">${select('profile-mode','Управление профилем',[
 ['auto','Автоматически подбирать под этот ПК'],['manual','Контекст и лимит ответа вручную']],p.profile_mode||'manual')}
 ${select('profile-priority','Приоритет',[
 ['quality','Качество с контролем скорости'],['balanced','Баланс'],['speed','Скорость']],p.priority||'quality')}
 ${input('profile-target','Желаемая скорость, ток/с',p.target_tps??20,'min="1" max="300" step="1" required')}
 <p class="prose">В автоматическом режиме AgentVisor проверяет память и скорость до начала задачи. Пустое поле модели разрешает выбор среди установленных моделей с инструментами. Значение скорости — цель, а не обещание.</p>
 <div class="form-actions"><button type="button" class="button" data-model-action="recommend">Предварительный подбор</button><button type="button" class="button" data-model-action="tune">Подобрать и проверить</button></div></div>`);
}

export function profileAdvanced(p){
 return markup(`<details class="profile-advanced"><summary>Размещение и генерация</summary>
 <p class="prose">Эти настройки отправляются в API модели. Ползунки в чате LM Studio не управляют запросами OpenCode.</p>
 ${select('profile-gpu','Размещение модели',[
 ['auto','Авто · выбор LM Studio'],['max','Полностью на GPU'],['off','Только CPU']],p.gpu||'auto')}
 <p class="prose">Ручное размещение доступно для локального LM Studio. При автоподборе учитывается запас памяти; при нехватке VRAM возможна более медленная работа через RAM.</p>
 ${select('profile-flash','Flash Attention',[['auto','Автоматически'],['on','Включено'],['off','Выключено']],p.flash_attention||'auto')}
 <div class="form-row">${select('profile-cache-k','Тип K-кэша',[['auto','Автоматически'],['f16','F16 · без квантования'],['q8_0','Q8 · экономия памяти'],['q4_0','Q4 · сильное сжатие']],p.cache_type_k||'auto')}${select('profile-cache-v','Тип V-кэша',[['auto','Автоматически'],['f16','F16 · без квантования'],['q8_0','Q8 · экономия памяти'],['q4_0','Q4 · сильное сжатие']],p.cache_type_v||'auto')}</div>
 <p class="prose">Flash Attention и KV-кэш настраиваются при загрузке локального LM Studio. Автоподбор пробует Q8 при нехватке видеопамяти. Q4 доступен вручную и может сильнее влиять на точность. Квантование V-кэша требует Flash Attention.</p>
 ${select('profile-reasoning','Рассуждение',[['auto','Авто · по приоритету'],...Object.entries(reasoningLabels)],p.reasoning||'auto')}
 <p class="prose" id="reasoning-help">Доступные режимы определяются выбранной моделью. Более глубокое рассуждение может увеличить ожидание ответа.</p>
 <div class="form-row">${input('profile-temperature','Температура',p.temperature,'min="0" max="2" step="0.05" placeholder="Авто"')}${input('profile-top-p','Top P',p.top_p,'min="0.01" max="1" step="0.01" placeholder="Авто"')}</div>
 ${input('profile-top-k','Top K',p.top_k,'min="0" max="200" step="1" placeholder="Авто"')}
 <p class="prose">Пустые параметры выборки оставляют настройки сервера модели. Изменение температуры не добавляет модели знаний.</p>
 <label class="check-label"><input id="profile-watchdog" type="checkbox" ${p.watchdog!==false?'checked':''}><span>Следить за моделью во время задачи и восстанавливать при сбое</span></label>
 <p class="prose">Проверка каждые 5 секунд. Два сбоя подряд запускают восстановление с сохранением документов. На паузе модель не поднимается.</p></details>`);
}

export function readStrategy(){
 const value=id=>document.getElementById(id).value;
 const nullable=id=>value(id)===''?null:Number(value(id));
 return {profile_mode:value('profile-mode'),priority:value('profile-priority'),target_tps:Number(value('profile-target')),
 gpu:value('profile-gpu'),reasoning:value('profile-reasoning'),temperature:nullable('profile-temperature'),
 top_p:nullable('profile-top-p'),top_k:nullable('profile-top-k'),watchdog:document.getElementById('profile-watchdog').checked,
 flash_attention:value('profile-flash'),cache_type_k:value('profile-cache-k'),cache_type_v:value('profile-cache-v')};
}

export function bindStrategy(context){
 const find=id=>document.getElementById(id);
 function update(){
  const automatic=find('profile-mode').value==='auto';
  find('profile-context').disabled=automatic;find('profile-output').disabled=automatic;
  const model=context.state.modelInfo?.models?.find(model=>model.id===find('profile-model').value.trim());
  const selected=find('profile-reasoning').value;
  const allowed=model?.reasoning?.allowed_options||[];
  find('profile-reasoning').innerHTML=`<option value="auto">${esc(txt('Авто · по приоритету'))}</option>${allowed.map(value=>`<option value="${esc(value)}">${esc(txt(reasoningLabels[value]||value))}</option>`).join('')}${selected!=='auto'&&!allowed.includes(selected)?`<option value="${esc(selected)}">${esc(selected)} · ${esc(txt('не подтверждено'))}</option>`:''}`;
  find('profile-reasoning').value=selected;
 }
 find('profile-form').addEventListener('change',update);find('profile-model').addEventListener('input',update);
 update();return update;
}

export function planText(plan){
 const p=plan.profile,estimate=plan.estimate||{},sample=plan.samples?.findLast(s=>s.profile.model===p.model&&s.profile.context===p.context);
 const actual=plan.load_config||sample?.load_config;
 return [txt(plan.reason),tr`Модель: ${p.model}`,tr`Контекст: ${number(p.context)}`,tr`Максимум ответа: ${number(p.output_limit)}`,
 tr`Рассуждение: ${txt(reasoningLabels[p.reasoning]||'Авто')}`,tr`Размещение: ${p.gpu}`,
 tr`Flash Attention: ${txt(p.flash_attention==='on'?'Включено':p.flash_attention==='off'?'Выключено':'Авто')}`,
 tr`K / V кэш: ${p.cache_type_k||'auto'} / ${p.cache_type_v||'auto'}`,
 estimate.gpu_bytes!==undefined?(estimate.defaults_only?tr`Оценка VRAM с настройками runtime по умолчанию: ${gigabytes(estimate.gpu_bytes)} ГБ`:tr`Оценка VRAM: ${gigabytes(estimate.gpu_bytes)} ГБ`):'',
 actual?tr`Подтверждено после загрузки: FA=${actual.flashAttention}, K=${actual.llamaKCacheQuantizationType}, V=${actual.llamaVCacheQuantizationType}`:'',
 typeof actual?.gpu?.ratio==='number'?tr`Доля загрузки на GPU по данным runtime: ${number(actual.gpu.ratio*100)}%`:'',
 sample?.memory_after?.gpu_free?tr`Свободно VRAM после замера: ${gigabytes(sample.memory_after.gpu_free)} ГБ`:'',
 sample?tr`Измеренная скорость генерации: ${number(sample.generation_tps||sample.request_tps)} ток/с`:'',
 ...(plan.warnings||[]).map(txt),txt('Это подбор по ресурсам и скорости. Качество решения вашей задачи требует отдельной проверки.')].filter(Boolean).join('\n');
}
