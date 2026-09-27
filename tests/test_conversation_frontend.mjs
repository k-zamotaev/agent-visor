// Real module interactions with a minimal DOM; rendered layout is checked in-browser.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {renderMarkdown,mergeItems,submissionAttempt,composerLimit,timestamp} from '../agentvisor/static/conversation_format.js';

assert.equal(renderMarkdown('<script>alert(1)</script>'),'<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>');
assert.equal(renderMarkdown('**Bold** and `x < y`'),'<p><strong>Bold</strong> and <code>x &lt; y</code></p>');
assert.match(renderMarkdown('[Safe](https://example.com/?x="a")'),/href="https:\/\/example.com\/\?x=%22a%22"/);
for (const source of ['[bad](javascript:alert(1))','[bad](data:text/html,evil)','[bad](file:///etc/passwd)','![bad](javascript:x)']) assert.doesNotMatch(renderMarkdown(source),/<a |<img /);
assert.match(renderMarkdown('```html\n<img src=x onerror=alert(1)>\n```'),/<pre><code data-language="html">&lt;img/);
assert.doesNotMatch(renderMarkdown('`[bad](https://example.org)`'),/<a /);
assert.equal(renderMarkdown('- one\n- two\n\n1. first\n2. second'),'<ul><li>one</li><li>two</li></ul><ol><li>first</li><li>second</li></ol>');
assert.equal(renderMarkdown('```\nunclosed'),'<pre><code>unclosed</code></pre>');
assert.equal(timestamp(NaN),0);
assert.deepEqual(mergeItems([{id:2,time:2,text:'new',updated_at:10}],[{id:2,time:2,text:'old',updated_at:8},{id:1,time:1}]),[{id:1,time:1},{id:2,time:2,text:'new',updated_at:10}]);
const attempt = submissionAttempt({text:'  Save changes  ',kind:'instruction'},()=>'fixed-id');
assert.equal(submissionAttempt({text:'Save changes',kind:'instruction',attempt},()=>assert.fail('Retries must reuse their id')),attempt);
assert.notEqual(submissionAttempt({text:'Changed',kind:'instruction',attempt},()=>'new-id'),attempt);
assert.equal(composerLimit({chars_remaining:3},'four').canSend,false);
assert.equal(composerLimit({messages_remaining:0},'hello').canSend,false);
assert.equal(composerLimit({can_send:false},'hello').canSend,false);
assert.equal(composerLimit({},'hello').canSend,true);
assert.equal(composerLimit({message_max_chars:2},'😀😀').canSend,true);

class Element {
 constructor(tag='div') { this.tagName=tag; this.children=[]; this.dataset={}; this.style={}; this.attributes={}; this.listeners={}; this.hidden=false; this.disabled=false; this.value=''; this.checked=false; this._scrollTop=0; this.clientHeight=200; this.textValue=''; this.html=''; this.open=false;
  this.classList={toggle:(name,enabled)=>{const classes=new Set(this.className?.split(' ').filter(Boolean)); if(enabled)classes.add(name);else classes.delete(name);this.className=[...classes].join(' ');}};
 }
 set textContent(text) { this.textValue=String(text); this.children=[]; }
 get textContent() { return this.textValue+this.children.map(node=>node.textContent).join(''); }
 set innerHTML(html) { this.html=html; this.children=[]; }
 get innerHTML() { return this.html; }
 get lastElementChild() { return this.children.at(-1); }
 get scrollTop() { return this._scrollTop; }
 set scrollTop(value) { this._scrollTop=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight)); }
 layoutHeight() { if(this.dataset.messageId || this.className==='conversation-date')return 90;if(this.className==='conversation-work-group')return 90+(this.open?this._parts.body.layoutHeight():0);return this.children.length?this.children.reduce((sum,child)=>sum+child.layoutHeight(),0):90; }
 get scrollHeight() { return Math.max(200,this.id==='conversation-scroll'?nodes['conversation-messages'].layoutHeight():this.layoutHeight()); }
 get isConnected() { return !!this.parentElement || !!this.id; }
 append(...children) { for(const child of children)this.insertBefore(child,null); }
 insertBefore(child,before) { child.remove(); const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,child);child.parentElement=this; }
 replaceChildren(...children) { for(const child of [...this.children])child.remove();this.append(...children); }
 remove() { if(this.parentElement){const children=this.parentElement.children;children.splice(children.indexOf(this),1);this.parentElement=null;} }
 contains(node) { return this===node || this.children.some(child=>child.contains(node)); }
 setAttribute(name,value) { this.attributes[name]=value; }
 querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
 querySelectorAll(selector) { const matches=node=>/^[a-z][a-z0-9]*$/.test(selector)?node.tagName===selector:selector==='[data-message-id]'?node.dataset.messageId!==undefined:selector==='[data-error-text]'?node.dataset.errorText!==undefined:selector==='[data-page="tasks"]'?node.dataset.page==='tasks':false;
  return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);
 }
 addEventListener(name,listener) { (this.listeners[name] ||= []).push(listener); }
 async fire(name,event={}) { for(const listener of this.listeners[name] || [])await listener({preventDefault(){},...event}); }
 focus() { document.activeElement=this; }
 getClientRects() { let child=this;for(let parent=this.parentElement;parent;parent=parent.parentElement){if(parent.tagName==='details'&&!parent.open&&parent.children[0]!==child)return [];child=parent;}return [{}]; }
 getBoundingClientRect() { if(!this.getClientRects().length)return {top:0,bottom:0,height:0};const index=this.parentElement?.children.indexOf(this) || 0;
  const top=this.id==='conversation-scroll'?0:this.id==='conversation-messages'?-nodes['conversation-scroll'].scrollTop:this.parentElement?this.parentElement.getBoundingClientRect().top+this.parentElement.children.slice(0,index).reduce((sum,node)=>sum+node.layoutHeight(),0):0;
  const height=this.id==='conversation-scroll'?this.clientHeight:this.layoutHeight();return {top,bottom:top+height,height}; }
}
const names=['conversation-scroll','conversation-messages','conversation-empty','conversation-loading','conversation-error','conversation-retry','conversation-older','conversation-latest','conversation-activity','message-form','message-text','message-kind','message-recheck','message-send','message-help','message-count','message-error'];
const nodes=Object.fromEntries(names.map(id=>{const node=new Element();node.id=id;return [id,node];}));
nodes['message-send'].append(new Element('span')); const errorText=new Element();errorText.dataset.errorText='';nodes['conversation-error'].append(errorText,nodes['conversation-retry']);
const emptyCreate=new Element('button');emptyCreate.dataset.page='tasks';nodes['conversation-empty'].append(new Element('h2'),new Element('p'),emptyCreate);
globalThis.document={getElementById:id=>nodes[id],createElement:tag=>new Element(tag),getSelection:()=>({isCollapsed:true}),activeElement:null,documentElement:{lang:'ru'},dispatchEvent(){}};
const storage=new Map();globalThis.sessionStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)};
globalThis.localStorage={getItem:()=>null};
const catalog=JSON.parse(await readFile(new URL('../agentvisor/static/locales/en.json',import.meta.url),'utf8'));
globalThis.fetch=async()=>({ok:true,json:async()=>catalog});
const observed=[];let resizeCallback;globalThis.ResizeObserver=class {constructor(callback){resizeCallback=callback;}observe(node){observed.push(node);}};
const {initConversation}=await import('../agentvisor/static/conversation.js');
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const sent=[],requests=[],history=new Map();let hold=null,failPost=false,failGet=false,refreshes=0,pagination=null;
const composer={can_send:true,message_max_chars:6000,chars_remaining:20000,messages_remaining:40,paused:true};
const api=async(path,options={})=>{
 requests.push(path);const id=path.split('/')[2];
 if(options.method==='POST') { sent.push({id,body:JSON.parse(options.body)});if(hold)await hold.promise;if(failPost)throw Error('Network interrupted');return {}; }
 if(failGet)throw Error('Offline');if(pagination && id==='pages')return pagination(path);return {items:history.get(id)||[],composer,next_before:null,has_more:false};
};
const conversation=initConversation({state:{},api,refresh:async()=>{refreshes++;}});
assert.deepEqual(observed,[nodes['message-form'],nodes['conversation-activity']]);
const task=id=>({id,status:'paused',checklist:[]});
conversation.update(task('a'));await settle();
assert.equal(nodes['conversation-empty'].querySelector('h2').textContent,'Переписка этой задачи');assert.equal(emptyCreate.hidden,true);
assert.equal(nodes['message-send'].disabled,true);
assert.match(nodes['message-help'].textContent,/после запуска или продолжения/);
nodes['message-text'].value='Draft for A';await nodes['message-text'].fire('input');
assert.equal(nodes['message-send'].disabled,false);
conversation.update(task('b'));await settle();
assert.equal(nodes['message-text'].value,'');
nodes['message-text'].value='Draft for B';await nodes['message-text'].fire('input');
conversation.update(task('a'));await settle();assert.equal(nodes['message-text'].value,'Draft for A');
assert.equal(JSON.parse(storage.get('agentvisor-message-draft:b')).text,'Draft for B');

// A failed request retains the draft and the exact idempotency key for a retry.
failPost=true;await nodes['message-form'].fire('submit');await settle();
assert.equal(nodes['message-text'].value,'Draft for A');assert.match(nodes['message-error'].textContent,/Network interrupted/);
const firstId=sent.at(-1).body.client_message_id;
nodes['message-text'].value='Draft temporarily edited';await nodes['message-text'].fire('input');
nodes['message-text'].value='Draft for A';await nodes['message-text'].fire('input');
failPost=false;await nodes['message-form'].fire('submit');await settle();
assert.equal(sent.at(-1).body.client_message_id,firstId);
assert.equal(nodes['message-text'].value,'');assert.equal(refreshes,1);
assert.ok(requests.every(path=>!path.includes('/resume')&&!path.includes('/start')),'Sending must not resume a paused task');

// Task switching while a send is pending never clears the destination draft.
nodes['message-text'].value='Pending for A';await nodes['message-text'].fire('input');hold=deferred();
const sending=nodes['message-form'].fire('submit');await settle();
conversation.update(task('b'));await settle();assert.equal(nodes['message-text'].value,'Draft for B');
hold.resolve();await sending;hold=null;await settle();assert.equal(nodes['message-text'].value,'Draft for B');
conversation.update(task('a'));await settle();assert.equal(nodes['message-text'].value,'');

// New typing while a send is pending is preserved even in the same task.
nodes['message-text'].value='Sent first';await nodes['message-text'].fire('input');hold=deferred();
const typingSend=nodes['message-form'].fire('submit');await settle();
nodes['message-text'].value='New draft';await nodes['message-text'].fire('input');
hold.resolve();await typingSend;hold=null;await settle();assert.equal(nodes['message-text'].value,'New draft');

// IME composition and Shift+Enter do not submit.
const count=sent.length;
await nodes['message-text'].fire('keydown',{key:'Enter',isComposing:true});
await nodes['message-text'].fire('keydown',{key:'Enter',keyCode:229});
await nodes['message-text'].fire('keydown',{key:'Enter',shiftKey:true});
await settle();assert.equal(sent.length,count);

// Stable messages retain their nodes and disclosure state through polling.
history.set('a',[{id:'reasoning:1',role:'assistant',kind:'reasoning',text:'A thought',time:1,actor:'executor'},{id:'event:2',role:'assistant',kind:'text',text:'**Result**',time:2}]);
conversation.activate();await settle();
const thought=nodes['conversation-messages'].querySelectorAll('[data-message-id]').find(node=>node.dataset.messageId==='reasoning:1');
assert.ok(thought);assert.equal(thought._parts.disclosure.open,false);assert.equal(thought._parts.body.innerHTML,'');
thought._parts.disclosure.open=true;await thought._parts.disclosure.fire('toggle');assert.match(thought._parts.body.innerHTML,/A thought/);
conversation.activate();await settle();assert.equal(nodes['conversation-messages'].querySelectorAll('[data-message-id]').find(node=>node.dataset.messageId==='reasoning:1'),thought);assert.equal(thought._parts.disclosure.open,true);

// A reader's selected text is not replaced by a streamed update.
document.getSelection=()=>({isCollapsed:false,anchorNode:thought._parts.body,focusNode:thought._parts.body});
history.get('a')[0]={...history.get('a')[0],text:'Updated thought',updated_at:3};conversation.activate();await settle();assert.match(thought._parts.body.innerHTML,/A thought/);
document.getSelection=()=>({isCollapsed:true});conversation.activate();await settle();assert.match(thought._parts.body.innerHTML,/Updated thought/);

// Loading errors remain retryable without damaging the composed message.
failGet=true;conversation.activate();await settle();assert.equal(nodes['conversation-error'].hidden,false);assert.match(errorText.textContent,/Offline/);assert.equal(nodes['message-text'].value,'New draft');
failGet=false;await nodes['conversation-retry'].fire('click');await settle();assert.equal(nodes['conversation-error'].hidden,true);
// Newly arriving text never pulls a reader away from older messages.
const message=index=>({id:'event:'+index,role:'assistant',kind:'text',text:'Message '+index,time:index});
history.set('a',Array.from({length:12},(_,index)=>message(index+20)));conversation.activate();await settle();
nodes['conversation-scroll'].scrollTop=180;await nodes['conversation-scroll'].fire('scroll');const readingTop=nodes['conversation-scroll'].scrollTop;
nodes['message-text'].focus();history.get('a').push(message(32));conversation.activate();await settle();
assert.equal(nodes['conversation-scroll'].scrollTop,readingTop);assert.equal(nodes['conversation-latest'].hidden,false);assert.equal(document.activeElement,nodes['message-text']);
await nodes['conversation-latest'].fire('click');assert.equal(nodes['conversation-latest'].hidden,true);assert.ok(nodes['conversation-scroll'].scrollTop>readingTop);

// Older pages are prepended once, retaining the visible message's position.
pagination=path=>path.includes('before=older-cursor')?{items:[3,4,5,6,7].map(message),composer,next_before:null,has_more:false}:
 {items:[8,9,10,11,12].map(message),composer,next_before:'older-cursor',has_more:true};
conversation.update(task('pages'));await settle();nodes['conversation-scroll'].scrollTop=0;
const firstVisible=nodes['conversation-messages'].children.find(node=>node.dataset.messageId==='event:8');const beforePosition=firstVisible.getBoundingClientRect().top;
await nodes['conversation-older'].fire('click');await settle();
assert.ok(requests.some(path=>path.includes('before=older-cursor')));assert.equal(firstVisible.getBoundingClientRect().top,beforePosition);
assert.equal(nodes['conversation-messages'].querySelectorAll('[data-message-id]').length,10);assert.equal(nodes['conversation-older'].hidden,true);
history.set('receipt',[{id:'context:4',role:'user',kind:'instruction',text:'Design the interface',state:'pending',time:1,updated_at:1,details:{version:4}}]);
conversation.update(task('receipt'));await settle();
const receipt=nodes['conversation-messages'].querySelectorAll('[data-message-id]')[0];assert.match(receipt._parts.state.textContent,/Ожидает/);
conversation.update({...task('receipt'),updated:20,user_instructions:[{version:4,state:'verified',step_ids:[]}]});
assert.equal(receipt._parts.state.textContent,'Выполнение проверено');
// Consecutive technical work becomes one stable collapsed group. Human messages,
// answers, task events and dates are explicit boundaries between runs.
const technical=(id,time,kind='tool')=>({id,role:'assistant',kind,text:id,time,actor:'executor'});
history.set('groups',[technical('work:1',10,'reasoning'),technical('work:2',11),technical('work:3',12),message(13),
 technical('work:5',14),{id:'user:6',role:'user',kind:'instruction',text:'A requirement',time:15},technical('work:7',16),
 {id:'status:8',role:'system',kind:'status',text:'Checking',time:17},technical('work:9',18),technical('work:10',86410)]);
conversation.update(task('groups'));await settle();
const groups=nodes['conversation-messages'].children.filter(node=>node.dataset.workGroupId);
assert.equal(groups.length,5);const group=groups[0],nested=group._parts.body.children[0];
assert.equal(group.dataset.workGroupId,'work:1');assert.equal(group.open,false);assert.equal(group._parts.label.textContent,'Действия агента · 3');
group.open=true;nested._parts.disclosure.open=true;await nested._parts.disclosure.fire('toggle');
nodes['conversation-scroll'].scrollTop=180;await nodes['conversation-scroll'].fire('scroll');const groupScroll=nodes['conversation-scroll'].scrollTop;
document.getSelection=()=>({isCollapsed:false,anchorNode:nested._parts.body,focusNode:nested._parts.body});
history.get('groups')[0]={...history.get('groups')[0],text:'Revised thought',updated_at:19};history.get('groups').splice(2,0,technical('work:added',11.5));
conversation.activate();await settle();
assert.equal(nodes['conversation-messages'].children.find(node=>node.dataset.workGroupId==='work:1'),group);assert.equal(group.open,true);
assert.equal(group._parts.body.children[0],nested);assert.equal(nested._parts.disclosure.open,true);assert.match(nested._parts.body.innerHTML,/work:1/);
assert.equal(group._parts.label.textContent,'Действия агента · 4');assert.equal(nodes['conversation-scroll'].scrollTop,groupScroll);
document.getSelection=()=>({isCollapsed:true});

// Object-shaped role metadata and composer resizing update the surrounding UI.
conversation.update({...task('groups'),status:'running',active_role:{name:'reviewer'}});assert.equal(nodes['conversation-activity'].textContent,'Рецензент проверяет результат');
conversation.update({...task('groups'),status:'running',active_role:{name:'diagnostician'}});assert.equal(nodes['conversation-activity'].textContent,'Агент разбирается с затруднением');
const panel=new Element();panel.getBoundingClientRect=()=>({top:0,bottom:800});panel.append(nodes['conversation-latest']);
nodes['message-form'].getBoundingClientRect=()=>({top:600,bottom:800});nodes['conversation-activity'].getBoundingClientRect=()=>({top:560,bottom:600});
resizeCallback();assert.equal(nodes['conversation-latest'].style.bottom,'252px');
nodes['conversation-activity'].hidden=true;resizeCallback();assert.equal(nodes['conversation-latest'].style.bottom,'212px');
conversation.update(null);assert.equal(nodes['conversation-empty'].querySelector('h2').textContent,'Задача начинается с разговора');assert.equal(emptyCreate.hidden,false);
console.log('Conversation formatting, delivery, draft, task-switch, IME and stable DOM checks passed');
