// Pure formatting tests. Browser interactions are verified separately through the UI.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const catalog=JSON.parse(await readFile(new URL('../agentvisor/static/locales/en.json',import.meta.url),'utf8'));
const preferences=new Map();
globalThis.localStorage={getItem:key=>preferences.get(key),setItem:(key,value)=>preferences.set(key,value)};
globalThis.document={documentElement:{lang:'ru'}};
globalThis.fetch=async url=>{assert.equal(url,'/static/locales/en.json');return {ok:true,json:async()=>catalog};};
const {t,tr,markup,getLanguage,setLanguage}=await import('../agentvisor/static/i18n.js');
const {number,duration,statuses}=await import('../agentvisor/static/ui.js');
const {profileStrategy,profileAdvanced,planText}=await import('../agentvisor/static/profile.js');

assert.equal(t('Обзор'),'Обзор');
setLanguage('en');
assert.equal(getLanguage(),'en');
assert.equal(preferences.get('agentvisor-language'),'en');
assert.equal(document.documentElement.lang,'en');
assert.equal(t('Обзор'),'Overview');
assert.equal(statuses.running,'Running');
assert.equal(number(12.5),'12.5');
assert.equal(duration(3661),'1 h 1 min');
assert.equal(tr`Итерация ${2} из ${40}`,'Iteration 2 of 40');
const userContent='Модель <script>user text</script>';
assert.equal(tr`<label>Модель</label><span>${userContent}</span>`,
             '<label>Model</label><span>'+userContent+'</span>');
assert.equal(markup('<button>Сохранить профиль</button>'),'<button>Save profile</button>');
assert.doesNotMatch(profileStrategy({})+profileAdvanced({}),/[А-Яа-яЁё]/);
const plan=planText({profile:{model:'local',context:65536,output_limit:8192,reasoning:'xhigh',gpu:'auto',flash_attention:'on',cache_type_k:'q8_0',cache_type_v:'q8_0'},
 reason:'Профиль выбран по памяти runtime и измеренной скорости.',warnings:['Точная оценка памяти недоступна. Использован ограниченный стартовый контекст.']});
assert.doesNotMatch(plan,/[А-Яа-яЁё]/);
assert.match(plan,/K \/ V cache: q8_0 \/ q8_0/);
assert.equal(markup('<button>Сохранить</button><button>Применить и перезапустить</button>'),
             '<button>Save</button><button>Apply and restart</button>');
assert.equal(tr`Последний короткий замер\nМодель: ${'local'}\nКонтекст: ${32768}\nСкорость запроса: ${50} ток/с\nСкорость генерации: ${'unknown'}\nДлительность: ${3} с\n\n${'original note'}`,
             'Last short benchmark\nModel: local\nContext: 32768\nRequest speed: 50 tok/s\nGeneration speed: unknown\nDuration: 3 s\n\noriginal note');
setLanguage('ru');
assert.equal(statuses.running,'В работе');
assert.equal(number(12.5),'12,5');
assert.equal(duration(3661),'1 ч 1 мин');
assert.equal(tr`Итерация ${2} из ${40}`,'Итерация 2 из 40');
assert.throws(()=>setLanguage('de'),/Unsupported language/);
console.log('Frontend localization checks passed');
