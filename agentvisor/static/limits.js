import {t as txt,tr} from './i18n.js';
import {$,number,duration,active,toast} from './ui.js';

const finished=task=>['succeeded','completed_unverified'].includes(task?.status);
export const budgetExhausted=task=>!!task&&(task.elapsed>=task.max_hours*3600||task.iteration>=task.max_iterations);

export function openLimits(){
 $('#limits-panel').open=true;
 $('#limit-hours').focus();
}

export function renderLimits(task){
 const panel=$('#limits-panel'),form=$('#limits-form');
 panel.hidden=!task;
 if(!task)return;
 $('#limits-summary').textContent=tr`${number(task.max_hours)} ч · ${task.max_iterations} итераций`;
 $('#limits-usage').textContent=tr`Использовано: ${duration(task.elapsed)} и ${task.iteration} итераций. Осталось: ${duration(Math.max(0,task.max_hours*3600-task.elapsed))} и ${Math.max(0,task.max_iterations-task.iteration)} итераций.`;
 panel.classList.toggle('budget-exhausted',budgetExhausted(task)&&!finished(task));
 if(form.dataset.taskId!==task.id){
  delete form.dataset.dirty;delete form.dataset.version;
  form.dataset.taskId=task.id;panel.open=false;$('#limits-error').hidden=true;
 }
 const version=`${task.max_hours}:${task.max_iterations}`;
 if(!form.dataset.dirty&&form.dataset.version!==version){
  $('#limit-hours').value=task.max_hours;$('#limit-iterations').value=task.max_iterations;
  form.dataset.version=version;
 }
 form.querySelectorAll('input,button').forEach(el=>el.disabled=finished(task)||!!form.dataset.saving);
 $('#limits-resume').hidden=active.has(task.status)||finished(task);
}

export function bindLimits(context){
 const form=$('#limits-form'),error=$('#limits-error');
 form.addEventListener('input',()=>{form.dataset.dirty='true';error.hidden=true;});
 form.addEventListener('submit',async event=>{
  event.preventDefault();
  const task=context.state.task;if(!task||finished(task))return;
  const resume=event.submitter.value==='resume';
  const values={max_hours:Number($('#limit-hours').value),max_iterations:Number($('#limit-iterations').value)};
  error.hidden=true;
  if(resume&&(values.max_hours*3600<=task.elapsed||values.max_iterations<=task.iteration)){
   error.textContent=txt('Для продолжения оба лимита должны быть больше уже использованных значений.');error.hidden=false;return;
  }
  form.dataset.saving='true';renderLimits(task);
  try{
   await context.api(`/tasks/${task.id}`,{method:'PATCH',body:JSON.stringify(values)});
   delete form.dataset.dirty;
   if(resume)await context.api(`/tasks/${task.id}/start`,{method:'POST'});
   toast(resume?txt('Лимиты сохранены. Задача продолжена.'):txt('Лимиты сохранены'));
  }catch(failure){error.textContent=failure.message;error.hidden=false;}
  finally{delete form.dataset.saving;await context.refresh();renderLimits(context.state.task);}
 });
}
