/* Compact local snapshot catalog; never calls global refresh, native tools or models. */
(() => {
  'use strict';
  const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const txt=value=>typeof value==='string'?value:'';
  const element=(tag,cls,value)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(value!==undefined)n.textContent=String(value);return n;};
  const stamp=value=>{const at=typeof value==='string'?Date.parse(value):NaN;return Number.isFinite(at)?new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Europe/Moscow'}).format(at)+' МСК':'Не сообщалось';};
  function mount({host,request,onOpen}={}) {
    if(!(host instanceof Element)||typeof request!=='function'||typeof window.CtdTaskProgress?.describe!=='function')throw new TypeError('host, request and task-progress companion required');
    const root=element('section','codex-all-progress');root.hidden=true;root.setAttribute('aria-label','Ход задач');
    const heading=element('h2','codex-all-progress__title','Ход активных задач');
    const controls=element('div','codex-all-progress__controls');
    const buttons={};for(const [scope,label] of [['active','Активные'],['all','Все задачи']]){const button=element('button','',label);button.type='button';button.dataset.progressScope=scope;button.addEventListener('click',()=>selectScope(scope));buttons[scope]=button;controls.append(button);}
    const notice=element('p','codex-all-progress__notice','Сведения ещё не получены');notice.setAttribute('role','status');
    const list=element('div','codex-all-progress__list');root.append(heading,controls,notice,list);host.append(root);
    let scope='active',active=false,destroyed=false,generation=0,timer=null,flight=null,queued=false,rows=new Map();
    const canRead=()=>active&&!destroyed&&!document.hidden;
    const clear=()=>{if(timer!==null)clearTimeout(timer);timer=null;};
    function invalidate(){generation++;clear();queued=false;if(flight)flight.abort.abort();}
    function controlsState(){heading.textContent=scope==='active'?'Ход активных задач':'Ход всех задач';for(const name of ['active','all'])buttons[name].setAttribute('aria-pressed',String(name===scope));}
    function render(data){
      const incoming=new Map();let unavailable=0;
      for(const item of data.items.slice(0,50)){
        if(!item||!UUID.test(txt(item.thread_id)))continue;
        if(item.available!==true){unavailable++;continue;}
        incoming.set(item.thread_id,item);
      }
      const partial=data.catalog_complete!==true||data.truncated===true||data.items.length>50||unavailable>0;
      if(partial){for(const [id,item] of rows)if(!incoming.has(id)&&incoming.size<50)incoming.set(id,{...item,__retained:true});}
      rows=incoming;list.replaceChildren();
      for(const [id,item] of rows){
        const card=element('button','codex-all-progress__card');card.type='button';card.dataset.progressThreadId=id;
        card.addEventListener('click',()=>{if(typeof onOpen==='function')onOpen(id);});
        const view=window.CtdTaskProgress.describe(item), top=element('div','codex-all-progress__card-heading');
        top.append(element('strong','',txt(item.title)||'Задача'),element('strong','codex-all-progress__percent',view.percent===null?'—':view.percent.toLocaleString('ru-RU',{maximumFractionDigits:2})+'%'));
        const state=element('p','codex-all-progress__runtime','Сейчас: '+view.runtime+(item.runtime_stale===true||item.__retained?' · статус устарел':''));
        const steps=element('p','codex-all-progress__steps',view.counts?view.counts+' этапов':Array.isArray(item.checkpoints)&&item.checkpoints.length?'Готовность этапов не сообщалась':'Этапы не заданы');
        const checks=element('p','codex-all-progress__checks');
        const results=Array.isArray(item.results)?item.results:[];
        checks.textContent=results.length?results.slice(0,2).map(r=>[txt(r.title),txt(r.status)].filter(Boolean).join(': ')).join(' · ')+(results.length>2?' · ещё '+(results.length-2)+' результата':''):'Проверки не сообщались';
        card.append(top,state,steps,checks);
        if(results.some(r=>r?.provenance==='agent_report'))card.append(element('small','codex-all-progress__provenance','Сообщено агентом · исполнение не проверено'));
        if(view.previous)card.append(element('small','codex-all-progress__provenance','Предыдущая фаза'));
        card.append(element('small','codex-all-progress__time','Источник: '+(txt(item.source)||'Не сообщался')+' · срез '+stamp(item.updated_at_msk||item.runtime_checked_at_msk||item.checked_at_msk)));
        list.append(card);
      }
      if(!rows.size)list.append(element('p','codex-all-progress__empty',partial?'Сведения о задачах неполные':scope==='active'?'Активных задач нет':'Доступных задач нет'));
      notice.textContent=(partial?'Показана часть доступных задач · ':'')+'Каталог проверен: '+stamp(data.checked_at_msk);
      root.classList.toggle('codex-all-progress--stale',partial);
    }
    async function update(){
      if(!canRead())return;clear();if(flight){queued=true;return;}
      const epoch=generation,selected=scope,abort=new AbortController(),own={abort};flight=own;queued=false;
      try{
        const data=await request('/task-progress?scope='+scope,{signal:abort.signal,etag:false,timeoutMs:6000});
        if(epoch!==generation||selected!==scope||!canRead())return;
        if(!data||data.schema!=='codex-task-progress-batch/1'||!Array.isArray(data.items))throw new Error('invalid_catalog');
        render(data);
      }catch(error){if(epoch===generation&&selected===scope&&canRead()){notice.textContent='Нет связи · сохранён последний срез каталога';root.classList.add('codex-all-progress--stale');}}
      finally{if(flight===own)flight=null;if(canRead()){if(queued){queued=false;update();}else timer=setTimeout(update,5000);}}
    }
    function selectScope(next){if(!['all','active'].includes(next))throw new RangeError('unsupported scope');if(destroyed||scope===next)return;invalidate();scope=next;rows=new Map();list.replaceChildren();notice.textContent='Проверяем каталог задач…';controlsState();if(canRead())update();}
    function start(){if(destroyed||active)return;active=true;root.hidden=false;invalidate();controlsState();if(canRead())update();}
    function stop(){if(destroyed)return;active=false;invalidate();root.hidden=true;}
    function visibility(){invalidate();if(canRead())update();}
    function destroy(){if(destroyed)return;stop();destroyed=true;document.removeEventListener('visibilitychange',visibility);root.remove();}
    document.addEventListener('visibilitychange',visibility);controlsState();
    return Object.freeze({start,stop,destroy,selectScope});
  }
  window.CtdAllTaskProgress=Object.freeze({mount});
})();
