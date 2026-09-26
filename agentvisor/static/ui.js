import {t as txt,tr,markup,locale,getLanguage} from './i18n.js';
export const $ = (selector, root = document) => root.querySelector(selector);
export const esc = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
export const number = value => new Intl.NumberFormat(locale(), {maximumFractionDigits:1}).format(value || 0);
export const gigabytes = value => number(value / 1024 ** 3);
export const time = value => new Date(value * 1000).toLocaleTimeString(locale(), {hour12:false});
export const duration = seconds => seconds < 60 ? tr`${Math.floor(seconds)} с` : seconds < 3600 ? tr`${Math.floor(seconds / 60)} мин` : tr`${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин`;
const paths = {
 home:'m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',
 list:'M9 6h12M9 12h12M9 18h12M3 5l1 1 2-2M3 11l1 1 2-2M3 17l1 1 2-2',
 cube:'m12 3 9 5v9l-9 5-9-5V8zm0 10v9M3 8l9 5 9-5M8 5l9 5',
 clock:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2',
 settings:'m9 3 1-1h4l1 3 3 1 3 2-1 3 1 3-2 3-3 1-1 3h-4l-1-3-3-1-2-3 1-3-1-3 3-2 3-1zM16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
 edit:'m15 4 5 5-11 11-6 1 1-6zm0 0 2-2 5 5-2 2',
 play:'m8 4 12 8-12 8z', pause:'M8 4v16M16 4v16', stop:'M5 5h14v14H5z',
 chart:'M3 3v18h18M7 16v-5M12 16V6M17 16V9',
 refresh:'M20 7a9 9 0 0 0-16-1M4 2v5h5M4 17a9 9 0 0 0 16 1M20 22v-5h-5',
 chip:'M6 6h12v12H6zM9 9h6v6H9zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4',
 arrow:'M4 12h16m-6-6 6 6-6 6', check:'m5 12 4 4L19 6', close:'m6 6 12 12M18 6 6 18',
 file:'M14 2H5v20h14V7zm0 0v6h5M8 12h8M8 16h8',
 alert:'m12 3 10 18H2zM12 9v5M12 17v1', plus:'M12 4v16M4 12h16',
 info:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 11v6M12 7v1',
 search:'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0m-2 5 6 6', download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5'
};
export const icon = name => `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
export function icons(root = document) { root.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); }); }
export function toast(message, error = false) {
 const el = $('#toast'); el.textContent = message; el.hidden = false; el.classList.toggle('error', error);
 clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.hidden = true; }, 7000);
}
const statusLabels = {draft:'Готова к запуску',preparing:'Подготовка',running:'В работе',recovering:'Восстановление',verifying:'Проверка',pausing:'Пауза…',paused:'На паузе',stopping:'Остановка…',stopped:'Остановлена',blocked:'Требует внимания',failed:'Ошибка',succeeded:'Проверка пройдена',completed_unverified:'Завершена агентом'};
export const statuses = new Proxy(statusLabels,{get:(labels,key)=>txt(labels[key])});
export const active = new Set(['preparing','running','recovering','verifying','pausing','stopping']);
export function badge(status) { return `<span class="badge ${['failed','blocked'].includes(status) ? 'warning' : active.has(status) || status === 'succeeded' ? 'success' : 'neutral'}">${esc(statuses[status] || status)}</span>`; }
export function updateReasoning(root, events, full = false) {
 for(const event of events){
  if(event.kind!=='reasoning')continue;
  const pre=root.querySelector(`#${full?'event':'recent'}-${event.id} .reasoning-entry pre`);
  if(pre&&pre.textContent!==event.message){const scroll=pre.scrollTop;pre.textContent=event.message;pre.scrollTop=scroll;}
 }
}
export function eventRows(events, full = false, selectedId = null, expandReasoning = false) {
 if (!events.length) return markup('<div class="empty small">События появятся после запуска.<span>Здесь сохраняются шаги, ошибки и восстановления.</span></div>');
 return events.slice().reverse().map(e => {
  const glyph=e.level==='error'?'alert':e.level==='warning'?'refresh':({running:'play',preparing:'chip',model_step:'cube',document_saved:'file',tool:'file',verification_finished:'check',succeeded:'check',paused:'pause',stopped:'stop',iteration_finished:'clock'})[e.kind]||'info';
  const selected=String(e.id)===String(selectedId);
  const reasoning=e.kind==='reasoning';
  const article=full||reasoning;
  const start=article?`<article id="${full?'event':'recent'}-${e.id}" tabindex="-1" class="event ${esc(e.level)} ${selected?'selected-event':''}">`:`<button type="button" class="event recent-event ${esc(e.level)}" data-event-id="${e.id}" aria-label="${esc(time(e.time)+' '+e.message+txt(' — открыть запись журнала'))}">`;
  const label=reasoning?txt('Рассуждение модели'):e.kind==='text'?txt('Ответ модели'):e.kind;
  const message=reasoning?`<details class="reasoning-entry" ${!full||selected||expandReasoning?'open':''}><summary>${txt('Рассуждение модели')}</summary><pre>${esc(e.message)}</pre></details>`:`<span class="event-message">${esc(e.message)}</span>`;
  return `${start}<span class="event-icon">${icon(glyph)}</span><time>${time(e.time)}</time><span class="event-body">${message}${full ? tr`<small>${esc(label)}</small><details ${selected&&!reasoning?'open':''}><summary>Подробности</summary><pre>${esc(JSON.stringify({time:new Date(e.time*1000).toISOString(),kind:e.kind,fragment_count:e.fragment_count,first_event_id:e.first_event_id,last_event_id:e.last_event_id,...e.data},null,2))}</pre></details>` : reasoning?tr`<button type="button" class="text-button" data-event-id="${e.id}">Открыть запись журнала</button>`:`<small>${e.level==='warning'?txt('Событие требует внимания'):e.kind==='model_step'?txt('Ответ модели'):txt('Открыть запись журнала')}</small>`}</span>${article?'</article>':'</button>'}`;
 }).join('');
}
export function chart(samples, annotations = [], measuredWidth = 720, generation = false) {
 if (!samples.length) return tr`<div class="empty chart-empty">${icon('chart')}<strong>Пока нет измерений</strong><span>${generation?txt('Измерения появятся после нового ответа модели.'):txt('После каждой итерации здесь появится её средняя скорость.')}</span></div>`;
 const max = Math.max(10, ...samples.map(s => s.rate)) * 1.15;
 const width = Math.max(240, Math.round(measuredWidth)), height=170, left=38, right=width-22;
 const x=i=>left+i/Math.max(1,samples.length-1)*(right-left), y=rate=>height-rate/max*130;
 const points=samples.map((s,i)=>`${x(i)},${y(s.rate)}`).join(' ');
 const lines=[0,1,2,3].map(i=>{const lineY=height-i/3*130;return `<line x1="${left}" x2="${right}" y1="${lineY}" y2="${lineY}" stroke="var(--line)"/><text x="${left-10}" y="${lineY+4}" text-anchor="end">${Math.round(max*i/3)}</text>`;}).join('');
 const labels=samples.filter((_,i)=>i===0||i===samples.length-1||i===Math.floor(samples.length/2)).map(s=>`<text x="${x(samples.indexOf(s))}" y="194" text-anchor="middle">${esc(s.label)}</text>`).join('');
 const markers=annotations.slice(-2).map(a=>{const i=Math.min(samples.length-1,Math.max(0,a.index)),px=x(i),py=y(samples[i].rate),boxX=Math.min(right-145,Math.max(left,px-50));return `<g class="recovery-marker"><title>${esc(a.title)}</title><path d="M${boxX+65} 42 L${px} ${py}" stroke="var(--amber)" fill="none"/><rect x="${boxX}" y="8" width="145" height="34" rx="5" fill="#fff8e8" stroke="#deb975"/><text x="${boxX+8}" y="29" class="recovery-label">${esc(a.label)}</text><circle cx="${px}" cy="${py}" r="4" fill="var(--amber)"/></g>`;}).join('');
 return tr`<svg class="speed-svg" viewBox="0 0 ${width} 205" preserveAspectRatio="none" role="img" aria-label="${generation?txt('Скорость генерации ответов модели'):txt('Средняя скорость завершённых итераций.')} ${esc(annotations.map(a=>a.title).join('. '))}">${lines}<polyline points="${points}" fill="none" stroke="var(--blue)" stroke-width="2.4" stroke-linejoin="round"/>${samples.length===1?`<circle cx="${left}" cy="${y(samples[0].rate)}" r="4" fill="var(--blue)"/>`:''}${labels}${markers}</svg>`;
}
