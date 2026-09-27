// Exercise the shipped handlers with controlled asynchronous responses. This
// covers app integration races that the conversation's DOM tests cannot see.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=await readFile(new URL('../agentvisor/static/app.js',import.meta.url),'utf8');
const renderSource=source.slice(source.indexOf('function renderOverview(){'),source.indexOf('\nasync function refreshModels'));
assert.ok(renderSource.startsWith('function renderOverview(){'));
const elements=new Map(),node=id=>{
 if(!elements.has(id))elements.set(id,{clientWidth:700,classList:{add(){},remove(){},toggle(){}},dataset:{}});
 return elements.get(id);
};
let updates=0,readingPosition=420;
const state={page:'history',task:null,events:[],system:null,profile:{}};
const renderContext={state,$:node,setText(){},renderLimits(){},workspace:{renderPlan(){},render(){}},
 conversation:{update(){updates++;readingPosition=0;}},txt:text=>text,tr:(parts,...values)=>parts.reduce((result,part,index)=>result+part+(values[index]??''),''),
 duration:String,number:String,time:String,icon:()=>'',chart:()=>'',statuses:{},active:new Set(),budgetExhausted:()=>false};
vm.runInNewContext(renderSource+'\nglobalThis.render=renderOverview;',renderContext);
renderContext.render();
state.page='settings';renderContext.render();
assert.equal(updates,0,'Background polling must not render a hidden conversation');
assert.equal(readingPosition,420,'Opening another page must preserve the reading position');
state.page='overview';renderContext.render();assert.equal(updates,1,'Returning to the workspace renders fresh task data');

const languageStart=source.indexOf("$('#language-picker').addEventListener('change',async event=>{");
const languageSource=source.slice(languageStart,source.indexOf('\nbindLimits(',languageStart));
assert.ok(languageStart>=0 && languageSource.endsWith('});'));
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const control=(id,root,value)=>({id,value,checked:false,closest:selector=>selector.includes(root)?{}:null});

async function exerciseLanguageChange({switchTask=false,switchPage=false}={}) {
 let handler,language='ru',navigations=0;
 const pending=deferred(),message=control('message-text','#message-form','Task A draft'),kind=control('message-kind','#message-form','instruction');
 const limit=control('max-hours','#limits-form','48');
 const work={open:false,closest:selector=>selector.includes('#conversation-messages')?{}:null};
 const plan={open:false,closest:selector=>selector.includes('#checklist')?{}:null};
 const legacy={open:true,closest:()=>null};
 const controls=[message,kind,limit],disclosures=[work,plan,legacy];
 const byId=new Map(controls.map(item=>[item.id,item]));
 const taskState={page:'overview',selected:'a',refreshing:false};
 const picker={value:'en',focus(){},addEventListener(type,listener){assert.equal(type,'change');handler=listener;}};
 const context={state:taskState,$:selector=>{assert.equal(selector,'#language-picker');return picker;},
  document:{querySelectorAll:selector=>selector==='.page details'?disclosures:controls,getElementById:id=>byId.get(id)},
  setLanguage:value=>{language=value;},refresh:()=>pending.promise,refreshModels:async()=>{},navigate:async page=>{navigations++;taskState.page=page;},toast:message=>assert.fail(message)};
 vm.runInNewContext(languageSource,context);
 const changing=handler({target:picker});await Promise.resolve();await Promise.resolve();
 // The language request is still waiting on the server while the user edits
 // their message, expands evidence, or selects a completely different task.
 message.value=switchTask?'Task B draft':'Task A newly typed text';kind.value='reference';
 work.open=true;plan.open=true;
 if(switchTask){taskState.selected='b';limit.value='12';}
 if(switchPage)taskState.page='history';
 pending.resolve();await changing;
 assert.equal(language,'en');assert.equal(picker.disabled,false);
 assert.equal(message.value,switchTask?'Task B draft':'Task A newly typed text','Language refresh must not overwrite the current draft');
 assert.equal(kind.value,'reference');assert.equal(work.open,true);assert.equal(plan.open,true);
 if(switchTask)assert.equal(limit.value,'12','A task switch must not restore the previous task limits');
 if(switchPage){assert.equal(taskState.page,'history');assert.equal(navigations,0,'A newer navigation must win over an older language request');}
 else assert.equal(navigations,1);
}
await exerciseLanguageChange();
await exerciseLanguageChange({switchTask:true});
await exerciseLanguageChange({switchPage:true});
// The inspector is a desktop column and a closed mobile drawer. Its accessible
// expanded state follows those actual CSS rules at startup and each breakpoint.
const workspaceSource=(await readFile(new URL('../agentvisor/static/workspace.js',import.meta.url),'utf8')).replace(/^import .*;\r?\n/gm,'').replace('export function initWorkspace','function initWorkspace');
function inspectorFixture(initialDesktop){
 let desktop=initialDesktop;const classes=new Set(),callbacks={},ui=new Map();
 const get=selector=>{
  if(!ui.has(selector))ui.set(selector,{attributes:{},listeners:{},setAttribute(name,value){this.attributes[name]=value;},
   addEventListener(name,callback){this.listeners[name]=callback;},querySelectorAll:()=>[],focus(){}});
  return ui.get(selector);
 };
 const body={classList:{contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name)}};
 const context={$:get,document:{body,addEventListener(){}},window:{addEventListener:(name,callback)=>{callbacks[name]=callback;}},
  matchMedia:()=>({matches:desktop}),txt:value=>value};
 vm.runInNewContext(workspaceSource+'\nglobalThis.workspace=initWorkspace({state:{}});',context);
 return {context,classes,get,resize(value){desktop=value;callbacks.resize();},expanded:()=>get('#inspector-toggle').attributes['aria-expanded']};
}
const mobile=inspectorFixture(false);assert.equal(mobile.expanded(),'false','A closed mobile inspector is not expanded');
mobile.context.workspace.showInspector();assert.equal(mobile.expanded(),'true');
mobile.resize(true);assert.equal(mobile.expanded(),'true');
mobile.classes.delete('inspector-open');mobile.resize(false);assert.equal(mobile.expanded(),'false');
const desktop=inspectorFixture(true);assert.equal(desktop.expanded(),'true','The default desktop inspector is visible');
desktop.classes.add('inspector-hidden');desktop.resize(true);assert.equal(desktop.expanded(),'false');
desktop.classes.add('inspector-open');desktop.resize(false);assert.equal(desktop.expanded(),'true','Mobile visibility follows the drawer state');
// body[data-page] stores the current surface; it is not a navigation control.
// Delegation must leave ordinary clicks (including the mobile toggle) alone.
const clickStart=source.indexOf("document.addEventListener('click',event=>{");
const clickSource=source.slice(clickStart,source.indexOf("\ndocument.addEventListener('conversation-event'",clickStart));
const navigation=[],clickState={};let clickHandler;
vm.runInNewContext(clickSource,{document:{addEventListener:(name,callback)=>{assert.equal(name,'click');clickHandler=callback;}},state:clickState,
 navigate:async page=>{navigation.push(page);},toast:message=>assert.fail(message),startDemo:()=>assert.fail('Unexpected demo')});
function clickElement(tag,dataset={},parent=null,id=''){
 return {tag,dataset,parent,id,closest(selector){
  for(let candidate=this;candidate;candidate=candidate.parent){
   for(const part of selector.split(',')){
    const match=part.match(/^(button|a)?\[data-([a-z-]+)\]$/);
    if(match && (!match[1] || match[1]===candidate.tag)){
     const key=match[2].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());if(Object.hasOwn(candidate.dataset,key))return candidate;
    }
    if(part[0]==='#'&&candidate.id===part.slice(1))return candidate;
   }
  }return null;
 }};
}
const pageBody=clickElement('body',{page:'overview'});
clickHandler({target:pageBody});
clickHandler({target:clickElement('button',{},pageBody,'sidebar-toggle')});
clickHandler({target:clickElement('summary',{},pageBody)});
clickHandler({target:clickElement('textarea',{},pageBody,'message-text')});
assert.equal(navigation.length,0,'Ordinary controls must not navigate or close the sidebar');
clickHandler({target:clickElement('path',{},clickElement('button',{page:'history'},pageBody))});
clickHandler({target:clickElement('span',{},clickElement('a',{page:'models'},pageBody))});
assert.deepEqual(navigation,['history','models']);
clickHandler({target:clickElement('button',{eventId:'42'},pageBody)});
assert.deepEqual(navigation,['history','models','history'],'A journal click must not be overridden by body navigation');
assert.equal(clickState.focusEvent,'42');
console.log('Workspace hidden polling and asynchronous language/task-switch checks passed');
