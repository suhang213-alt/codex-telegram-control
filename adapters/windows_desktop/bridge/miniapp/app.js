/* Telegram Mini App frontend. The server must verify Telegram initData on every request. */
(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const $ = (id) => document.getElementById(id);
  const MODEL_EFFORTS = {
    'gpt-6.1-sol': ['low','medium','high','xhigh','max','ultra'],
    'gpt-6-astra': ['low','medium','high','xhigh','max','ultra'],
    'gpt-6-sol': ['low','medium','high','xhigh','max','ultra'],
    'gpt-6-luna': ['low','medium','high','xhigh','max'],
    'gpt-5.6-sol': ['low','medium','high','xhigh','max','ultra'],
    'gpt-5.6-terra': ['low','medium','high','xhigh','max','ultra'],
    'gpt-5.6-luna': ['low','medium','high','xhigh','max']
  };
  const EFFORT_LABELS = {low:'Низкая',medium:'Средняя',high:'Высокая',xhigh:'Очень высокая',max:'Максимальная',ultra:'Ультра'};
  const state = {view:'chats',previous:'chats',surface:'codex',threads:[],projects:[],archived:[],usage:null,nextCursor:null,historyCursor:null,historyPending:false,historyOperationId:'',historyExpanded:false,filter:'all',search:'',selectedThread:null,threadMessages:[],artifacts:[],artifactsThreadId:null,artifactsComplete:false,artifactsReason:'',artifactsLoading:false,artifactsError:'',messageFiles:[],newFiles:[],model:'',thinking:'',busy:false,messageOperationId:'',taskOperationId:'',messageAttachmentIds:null,taskAttachmentIds:null,actionKeys:{},operations:[],recorder:null,recordedChunks:[],voiceText:'',refreshing:false,lastConnectionError:''};
  state.decisions=[];state.decisionsThreadId='';state.decisionId='';state.decisionError='';state.decisionLoading=false;state.decisionSubmitting=false;state.decisionDrafts=new Map();
  const deepLink=(()=>{const params=new URL(location.href).searchParams,thread=params.get('thread'),decision=params.get('decision');return thread&&/^[0-9a-f-]{36}$/i.test(thread)?{thread,decision:decision&&/^[\w-]{1,128}$/.test(decision)?decision:''}:null;})();
  let deepLinkPending=Boolean(deepLink),deepLinkNavigated=false,decisionRequest=null,decisionWatchTimer=0,decisionReturnFocus=null;
  const responseEtags = new Map();
  const lastFetch = {threads:0,projects:0,usage:0,archived:0,detail:0};
  let toastTimer;
  let connectionVersion=0,connectionResponses=0;
  let usagePromise=null,usageLoaded=false,lastUsageAttempt=0,usageStaleTimer;
  const usageStatus={busy:false,stale:false,refreshing:false,error:null,checkedAt:null,outcome:''};
  let taskProgress=null,threadDetailRequest=null,threadSelectionVersion=0;
  let kanban=null;
  let viewportFrame=0,viewportBaseline=0,keyboardObserved=false;
  let deliveryWatchTimer=0,deliveryWatchThread='',deliveryWatchSince=0;
  const compactChat=()=>matchMedia('(max-width:640px), (max-width:950px) and (max-height:450px)').matches||(innerWidth<=1100&&Math.min(Number(window.visualViewport?.height)||innerHeight,Number(tg?.viewportHeight)||innerHeight)<=450&&(matchMedia('(pointer:coarse)').matches||['ios','android'].includes(tg?.platform)));
  function closeThreadMenu(){ $('thread-actions').hidden=true;$('thread-actions-toggle').setAttribute('aria-expanded','false'); }
  function updateChatViewport(){
    if(viewportFrame)return;
    viewportFrame=requestAnimationFrame(()=>{
      viewportFrame=0;
      const vv=window.visualViewport,narrow=compactChat(),focused=document.activeElement===$('message-text');
      const heights=[Number(vv?.height),Number(tg?.viewportHeight)].filter(value=>Number.isFinite(value)&&value>0);
      const height=Math.max(1,heights.length?Math.min(...heights):Number(tg?.viewportStableHeight)||innerHeight),top=Math.max(0,Number(vv?.offsetTop)||0);
      viewportBaseline=Math.max(viewportBaseline,Number(tg?.viewportStableHeight)||0,innerHeight,height);
      if(focused&&height<viewportBaseline-100)keyboardObserved=true;
      const composing=narrow&&state.view==='thread'&&focused&&(!keyboardObserved||height<viewportBaseline-100);
      const timeline=$('thread-timeline'),scroll=timeline.scrollTop,pinned=timeline.scrollHeight-scroll-timeline.clientHeight<40;
      document.documentElement.style.setProperty('--chat-viewport-height',height+'px');document.documentElement.style.setProperty('--chat-viewport-top',top+'px');
      document.documentElement.style.setProperty('--chat-safe-top',Math.max(0,Number(tg?.contentSafeAreaInset?.top)||0,Number(tg?.safeAreaInset?.top)||0)+'px');
      document.documentElement.style.setProperty('--chat-safe-bottom',Math.max(0,Number(tg?.contentSafeAreaInset?.bottom)||0,Number(tg?.safeAreaInset?.bottom)||0)+'px');
      document.body.classList.toggle('chat-compact',narrow);
      document.body.classList.toggle('chat-composing',composing);
      if(composing){setProgressExpanded(false);closeThreadMenu();$('thread-model-controls').hidden=true;$('composer-settings-toggle').setAttribute('aria-expanded','false');}
      if(narrow){
        const input=$('message-text'),expanded=$('thread-progress')?.classList.contains('task-progress-expanded');input.style.height='44px';
        input.style.height=Math.min(!focused&&expanded&&height<450?44:Math.max(44,height*.22),96,input.scrollHeight)+'px';
        const header=document.querySelector('#thread-view .detail-header').getBoundingClientRect().height,composer=$('message-form').getBoundingClientRect().height,reserve=height<450?64:120;
        document.documentElement.style.setProperty('--chat-progress-max-height',Math.max(0,Math.min(height*.4,height-header-composer-reserve))+'px');
      }else $('message-text').style.height='';
      if(state.view==='thread')timeline.scrollTop=pinned?timeline.scrollHeight:scroll;
    });
  }
  function installChatViewport(){
    window.visualViewport?.addEventListener('resize',updateChatViewport);window.visualViewport?.addEventListener('scroll',updateChatViewport);
    window.addEventListener('resize',updateChatViewport);window.addEventListener('orientationchange',()=>{viewportBaseline=0;updateChatViewport();});
    tg?.onEvent?.('viewportChanged',updateChatViewport);tg?.onEvent?.('safeAreaChanged',updateChatViewport);tg?.onEvent?.('contentSafeAreaChanged',updateChatViewport);
    $('message-text').addEventListener('focus',()=>{keyboardObserved=false;updateChatViewport();});$('message-text').addEventListener('blur',updateChatViewport);
    $('message-text').addEventListener('input',updateChatViewport);
    updateChatViewport();
  }
  function syncTaskProgress(){
    if(taskProgress&&state.view==='thread'&&state.selectedThread){
      taskProgress.select(state.selectedThread.id);taskProgress.start();
    }else taskProgress?.stop();
  }
  function localProgressTarget(value){
    try{
      const url=new URL(String(value||''));
      if(url.protocol!=='http:'||!['127.0.0.1','localhost'].includes(url.hostname)||url.port!=='55336'||url.pathname!=='/'||url.username||url.password)return null;
      const ids=url.searchParams.getAll('thread');
      return {current:ids.length===1&&ids[0]===state.selectedThread?.id&&/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(ids[0])};
    }catch{return null;}
  }
  function setProgressExpanded(expanded){
    const host=$('thread-progress'),button=$('thread-progress-toggle');if(!host||!button)return;
    const changed=host.classList.contains('task-progress-expanded')!==expanded;
    host.classList.toggle('task-progress-expanded',expanded);button.setAttribute('aria-expanded',String(expanded));
    button.textContent=expanded?'Свернуть':'Прогресс';button.setAttribute('aria-label',expanded?'Свернуть прогресс этой задачи':'Показать прогресс этой задачи');
    host.querySelector('.codex-task-progress__compact')?.setAttribute('aria-expanded',String(expanded));
    if(changed)updateChatViewport();
  }
  function dismissChatKeyboard(){if(compactChat()&&document.activeElement===$('message-text')){$('message-text').blur();tg?.hideKeyboard?.();updateChatViewport();}}
  function openCurrentProgress(threadId){
    if(!threadId||state.selectedThread?.id!==threadId)return;
    dismissChatKeyboard();
    closeArtifacts();show('thread',{quiet:true});setProgressExpanded(true);
    const host=$('thread-progress');host.scrollTop=0;host.focus({preventScroll:true});
  }
  function messageBubble(message){
    const bubble=el('div','message-bubble'),pattern=/\[([^\]\n]+)\]\(<?(https?:\/\/[^\s<>()]+)>?(?:\s+"[^"\n]*")?\)|<(https?:\/\/[^\s<>]+)>|(https?:\/\/[^\s<>()]+)/g;
    let offset=0;
    for(const match of message.matchAll(pattern)){
      const raw=match[2]||match[3]||match[4],url=match[4]?raw.replace(/[.,;:!?]+$/,''):raw;
      const board=window.CtdKanban?.localBoardTarget(url);
      if(!board&&!localProgressTarget(url)?.current)continue;
      bubble.append(document.createTextNode(message.slice(offset,match.index)));
      const button=el('button','message-progress-link',board?'Открыть Kanban проекта':'Прогресс этой задачи');button.type='button';
      const threadId=state.selectedThread?.id;button.addEventListener('click',()=>board?show('kanban'):openCurrentProgress(threadId));bubble.append(button);offset=match.index+match[0].length-(match[4]?raw.length-url.length:0);
    }
    bubble.append(document.createTextNode(message.slice(offset)));return bubble;
  }

  function notify(message, error=false) {
    const box=$('toast'); box.textContent=message; box.classList.toggle('error',error); box.classList.add('visible');
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>box.classList.remove('visible'),4000);
    if(error) tg?.HapticFeedback?.notificationOccurred?.('error');
  }
  function el(tag, className, text) {
    const node=document.createElement(tag); if(className) node.className=className; if(text!==undefined) node.textContent=String(text); return node;
  }
  function setConnection(value, description) {
    const phase=value===true?'checked':value===false?'error':value;
    const element=$('connection'),labels={checked:'Проверено',checking:'Проверка',unknown:'Не проверено',error:'Нет связи'};
    element.dataset.state=phase;element.classList.remove('online');element.classList.toggle('checked',phase==='checked');element.classList.toggle('error',phase==='error');
    element.querySelector('span').textContent=labels[phase]||labels.unknown;
    const detail=description||(phase==='checked'?'Ответ компьютера получен '+usageTimestamp(Date.now()/1000)+'. Это время последней проверки; постоянного соединения нет.':phase==='checking'?'Проверяем доступность компьютера…':phase==='error'?'Связь не подтверждена. Закройте панель и откройте её заново кнопкой Desktop в Telegram.':'Доступность компьютера пока не проверена. Нажмите «Обновить».');
    element.title=detail;element.setAttribute('aria-label',(labels[phase]||labels.unknown)+'. '+detail);$('connection-detail').textContent=detail;
  }
  function connectionError(error){connectionVersion++;setConnection(false,error.message+'. Закройте панель и откройте её заново кнопкой Desktop в Telegram.');if(state.lastConnectionError!==error.message){notify(error.message,true);state.lastConnectionError=error.message;}}
  function usageWindows(){
    return ['primary','secondary'].map(key=>state.usage?.[key]).filter(item=>item&&Number.isFinite(item.usedPercent)&&Number.isFinite(item.windowDurationMins)&&Number.isFinite(item.resetsAt))
      .map(item=>({...item,remaining:Math.max(0,Math.min(100,100-item.usedPercent))})).sort((a,b)=>a.windowDurationMins-b.windowDurationMins);
  }
  function usageDuration(minutes){
    if(minutes>=1440&&minutes%1440===0)return minutes/1440+' дн.';
    if(minutes>=60&&minutes%60===0)return minutes/60+' ч.';
    return minutes+' мин.';
  }
  function usageReset(seconds){return new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(seconds*1000))+' МСК';}
  function usageTimestamp(seconds){return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(seconds*1000))+' МСК';}
  function renderUsage(){
    const card=document.querySelector('.usage-card'),strip=$('usage-strip'),windows=usageWindows(),current=[...windows].sort((a,b)=>a.remaining-b.remaining)[0];
    const updated=Number(state.usage?.updated_at||0),hasTimestamp=Number.isFinite(updated)&&updated>0,stale=Boolean(usageStatus.error)||usageStatus.stale||!hasTimestamp||Date.now()<updated*1000||Date.now()-updated*1000>=180000;
    clearTimeout(usageStaleTimer);
    if(!stale)usageStaleTimer=setTimeout(renderUsage,Math.min(2147483647,Math.max(1,updated*1000+180000-Date.now()+1)));
    const refreshButton=$('usage-refresh'),refreshStatus=$('usage-refresh-status');
    refreshButton.disabled=usageStatus.busy;refreshButton.textContent=usageStatus.busy?'Обновляем…':'Обновить лимиты';
    refreshStatus.classList.toggle('error',Boolean(usageStatus.error));
    refreshStatus.textContent=usageStatus.busy?'Запрашиваем актуальные лимиты Codex…':usageStatus.error?'Не удалось обновить лимиты. '+(current?'Показаны последние полученные данные.':'Повторите запрос кнопкой выше.'):usageStatus.refreshing?'Обновление уже выполняется. Повторите запрос кнопкой выше.':usageStatus.outcome==='updated'?'Данные получены из Codex.':usageStatus.outcome==='cached'?'Показан последний полученный снимок.':'';
    if(usageStatus.error&&Number.isFinite(usageStatus.checkedAt)&&usageStatus.checkedAt>0)refreshStatus.textContent+=' Последние полученные данные: '+usageTimestamp(usageStatus.checkedAt)+'.';
    card.classList.toggle('stale',stale);strip.classList.toggle('stale',stale);
    const detail=$('usage-windows');detail.replaceChildren();
    if(!current){
      $('usage-remaining').textContent='—';$('usage-bar').style.width='0%';$('usage-updated').textContent='Данные пока недоступны';
      $('usage-note').textContent='Нажмите «Обновить лимиты», чтобы запросить данные Codex.';card.querySelector('[role="progressbar"]').setAttribute('aria-valuenow','0');
      $('usage-strip-value').textContent='—';$('usage-strip-window').textContent='Данные недоступны';$('usage-detail-updated').textContent='Данные пока недоступны';
      detail.append(el('div','empty-state','Не удалось получить остаток лимита. Проверьте подключение компьютера.'));return;
    }
    const format=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1});
    $('usage-strip-value').textContent=format.format(current.remaining)+'%';$('usage-strip-window').textContent=usageDuration(current.windowDurationMins)+(usageStatus.busy?' · обновляем':usageStatus.error?' · не обновлён':stale?' · устарело':'');
    strip.setAttribute('aria-label','Остаток лимита Codex '+format.format(current.remaining)+' процентов'+(stale?'. Последний снимок, актуальность не подтверждена':'')+'. Открыть подробности');
    $('usage-remaining').textContent=format.format(current.remaining)+'%';$('usage-bar').style.width=current.remaining+'%';card.querySelector('[role="progressbar"]').setAttribute('aria-valuenow',String(current.remaining));
    const checkedText=(usageStatus.error?'Обновление не удалось · последний снимок · ':stale?'Данные устарели · ':'')+(hasTimestamp?'Получены '+usageTimestamp(updated):'Время получения неизвестно');$('usage-updated').textContent=checkedText;$('usage-detail-updated').textContent=checkedText;
    $('usage-note').textContent='Наиболее ограниченный лимит · окно '+usageDuration(current.windowDurationMins)+' · сброс '+usageReset(current.resetsAt)+'.';
    windows.forEach(item=>{
      const row=el('article','usage-window-card');const head=el('div','usage-window-head');const title=el('div','');title.append(el('small','overline','ОКНО '+usageDuration(item.windowDurationMins)),el('h2','',usageDuration(item.windowDurationMins)));
      head.append(title,el('strong','',format.format(item.remaining)+'%'));const track=el('div','usage-window-track');const fill=el('span','');fill.style.width=item.remaining+'%';track.append(fill);
      row.append(head,track,el('p','','Осталось '+format.format(item.remaining)+'% · использовано '+format.format(item.usedPercent)+'%'),el('p','','Сброс: '+usageReset(item.resetsAt)));detail.append(row);
    });
  }
  async function request(path, options={}) {
    if(!tg?.initData) throw new Error('Откройте панель из Telegram');
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),options.timeoutMs||12000);
    const abortFromCaller=()=>controller.abort();
    if(options.signal){options.signal.addEventListener('abort',abortFromCaller,{once:true});if(options.signal.aborted)controller.abort();}
    const headers=new Headers(options.headers||{}); headers.set('X-Telegram-Init-Data',tg.initData);
    const method=options.method||'GET';
    if(method==='GET'&&options.etag!==false&&responseEtags.has(path))headers.set('If-None-Match',responseEtags.get(path));
    if(options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
    let serverReplied=false;
    try {
      const init={method,headers,body:options.body instanceof FormData?options.body:options.body?JSON.stringify(options.body):undefined,signal:controller.signal,cache:'no-store',credentials:'omit'};
      let response=await fetch('/api'+path,init);
      // One bounded retry is safe for reads when a temporary tunnel returns a gateway error.
      // Never replay a write: its first attempt may already have reached Desktop.
      if(method==='GET'&&[502,503,504].includes(response.status)&&!controller.signal.aborted){
        await new Promise(resolve=>setTimeout(resolve,250));
        response=await fetch('/api'+path,init);
      }
      if(response.status===304){if(options.connection!==false)connectionResponses++;return {notModified:true};}
      if(!response.ok){let detail='';try{const payload=await response.json();detail=payload.error||payload.message||'';serverReplied=response.status>=400&&response.status<500&&![401,403].includes(response.status);}catch{}const error=new Error(detail||([502,503,504].includes(response.status)?'Связь с панелью прервалась. Закройте её и снова откройте «Desktop» в боте.':`Ошибка сервера ${response.status}`));error.status=response.status;throw error;}
      if(method==='GET'&&response.headers.has('ETag'))responseEtags.set(path,response.headers.get('ETag'));
      const payload=response.status===204?{}:await response.json();serverReplied=true;if(options.connection!==false)connectionResponses++;return payload;
    } catch(error) {if(options.signal?.aborted)throw error;const failure=error.name==='AbortError'?new Error('Сервер не ответил вовремя'):['TypeError','SyntaxError'].includes(error.name)?new Error('Компьютер недоступен по текущему адресу'):error;if(!serverReplied&&options.connection!==false)connectionError(failure);throw failure;}
    finally {clearTimeout(timer);options.signal?.removeEventListener('abort',abortFromCaller);}
  }
  async function downloadAttachment(file) {
    if(!tg?.initData) throw new Error('Откройте панель из Telegram');
    const id=String(file.id||file.fileId||'');if(!id)throw new Error('Файл недоступен');
    const response=await fetch('/api/files/'+encodeURIComponent(id),{headers:{'X-Telegram-Init-Data':tg.initData},cache:'no-store',credentials:'omit'});
    if(!response.ok)throw new Error('Не удалось получить файл');
    const blob=await response.blob();const url=URL.createObjectURL(blob);
    try{const link=document.createElement('a');link.href=url;link.download=String(file.name||'download').replace(/[\\/]/g,'_');document.body.append(link);link.click();link.remove();}
    finally{setTimeout(()=>URL.revokeObjectURL(url),1000);}
  }
  function formatArtifactSize(value){
    if(!Number.isFinite(value)||value<0)return '';
    const units=['Б','КБ','МБ','ГБ'];let size=value,unit=0;
    while(size>=1024&&unit<units.length-1){size/=1024;unit++;}
    return new Intl.NumberFormat('ru-RU',{maximumFractionDigits:unit?1:0}).format(size)+' '+units[unit];
  }
  function artifactReason(value){
    const reason=String(value||'').trim();
    const labels={
      file_missing:'Исходный файл больше недоступен',secret_blocked:'Файл скрыт для защиты секретов',
      unsupported_reference:'Формат ссылки не поддерживается',invalid_reference:'Некорректная ссылка на результат',
      relative_without_workspace:'Не удалось определить рабочую папку файла',not_a_file:'Указанный результат не является файлом',
      inline_artifact_unavailable:'Встроенный артефакт недоступен',inline_artifact_invalid:'Встроенный артефакт повреждён',
      rollout_unavailable:'Локальная история задачи недоступна',rollout_incomplete:'Последняя запись задачи ещё формируется',
      rollout_parse_error:'Часть истории задачи не удалось прочитать',native_history_unavailable:'История результатов сейчас недоступна',
      native_history_partial:'Получена только часть истории результатов',native_cursor_unavailable:'Получена только часть истории результатов',
      outside_allowed_roots:'Файл находится вне разрешённых папок'
    };
    return labels[reason]||reason;
  }
  function resetArtifacts(threadId=null){
    state.artifacts=[];state.artifactsThreadId=threadId;state.artifactsComplete=false;state.artifactsReason='';state.artifactsLoading=false;state.artifactsError='';
    renderArtifacts();
  }
  function renderArtifacts(){
    const list=$('artifact-list'),status=$('artifact-status');if(!list||!status)return;
    status.classList.toggle('error',Boolean(state.artifactsError));
    status.textContent=state.artifactsLoading?'Получаем полный список файлов и артефактов…':state.artifactsError||(!state.artifactsComplete&&artifactReason(state.artifactsReason))||(!state.artifacts.length?'Артефакты пока не найдены':state.artifacts.length.toLocaleString('ru-RU')+' '+(state.artifacts.length===1?'артефакт':'артефакта'));
    if(state.artifactsLoading&&!state.artifacts.length){list.replaceChildren(el('div','artifact-empty','Загружаем результаты задачи…'));return;}
    const cards=state.artifacts.map(artifact=>{
      const card=el('article','artifact-card');card.dataset.artifactId=String(artifact.id||'');
      const icon=el('span','artifact-icon',artifact.kind==='link'?'↗':artifact.kind==='file'?'↓':'!');
      const copy=el('div','artifact-copy');copy.append(el('strong','',String(artifact.name||'Без названия')));
      const details=[artifact.kind==='file'?'Файл':artifact.kind==='link'?'Ссылка':'Недоступен',formatArtifactSize(Number(artifact.size)),artifact.mimeType].filter(Boolean).join(' · ');
      copy.append(el('small','artifact-meta',details));
      if(artifact.kind==='unavailable')copy.append(el('small','artifact-reason',artifactReason(artifact.reason)||'Оригинал недоступен'));
      const actions=el('div','artifact-actions');
      if(artifact.kind==='file'){
        const button=el('button','artifact-action','Скачать');button.type='button';button.addEventListener('click',()=>downloadArtifact(artifact,button));actions.append(button);
      }else if(artifact.kind==='link'){
        const progress=localProgressTarget(artifact.url);
        if(window.CtdKanban?.localBoardTarget(artifact.url)){
          const button=el('button','artifact-action','Открыть Kanban проекта');button.type='button';button.addEventListener('click',()=>{closeArtifacts();show('kanban');});actions.append(button);
        }else if(progress){
          if(progress.current){const button=el('button','artifact-action','Открыть прогресс');button.type='button';const threadId=state.selectedThread.id;button.addEventListener('click',()=>openCurrentProgress(threadId));actions.append(button);}
          else copy.append(el('small','artifact-reason','Ссылка не подтверждает текущую задачу. Прогресс выбранного чата показан над перепиской.'));
        }else try{const url=new URL(String(artifact.url||''));if(!['http:','https:'].includes(url.protocol))throw new Error();const link=el('a','artifact-action','Открыть');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);}catch{copy.append(el('small','artifact-reason','Ссылка недоступна'));}
      }
      card.append(icon,copy);if(actions.childElementCount)card.append(actions);return card;
    });
    list.replaceChildren(...(cards.length?cards:[el('div','artifact-empty','Артефакты пока не найдены')]));
  }
  function validatedTicketUrl(value){
    const url=new URL(String(value||''),location.href);
    if(url.protocol!=='https:'||url.origin!==location.origin||!url.pathname.startsWith('/api/artifact-download/'))throw new Error('Сервер вернул некорректную ссылку на файл');
    return url.href;
  }
  function isTelegramDownloadContext(){
    return Boolean(tg?.initData||(tg?.platform&&tg.platform!=='unknown'));
  }
  function offerBrowserDownload(button,url,expiresAt){
    const actions=button.parentElement;if(!actions)return;
    actions.querySelector('.artifact-browser-download')?.remove();
    const fallback=el('button','artifact-action artifact-browser-download','Скачать в браузере');
    fallback.type='button';
    fallback.addEventListener('click',()=>{
      if(Number(expiresAt)*1000<=Date.now()){
        notify('Ссылка истекла. Нажмите «Скачать» ещё раз, чтобы подготовить новую.',true);return;
      }
      if(typeof tg?.openLink!=='function'){
        notify('Telegram не поддерживает открытие браузера. Обновите Telegram и повторите скачивание.',true);return;
      }
      try{
        tg.openLink(validatedTicketUrl(url));
        notify('Файл открыт для скачивания в браузере. Панель и чат не изменены.');
      }catch{notify('Не удалось открыть браузер. Повторите скачивание или обновите Telegram.',true);}
    });
    actions.append(fallback);
  }
  function downloadFile(url,name,button,expiresAt){
    if(isTelegramDownloadContext()){
      // A new owner click preserves user activation and never replaces this WebView.
      offerBrowserDownload(button,url,expiresAt);
      if(typeof tg?.downloadFile==='function'&&tg?.isVersionAtLeast?.('8.0')){
        try{
          notify('Подтвердите скачивание в окне Telegram.');
          tg.downloadFile({url,file_name:name},accepted=>{
            if(accepted===false)notify('Скачивание отменено');
            else if(accepted===true)notify('Telegram принял запрос на скачивание.');
          });
          return;
        }catch{/* Keep the explicit external-browser action; never navigate this panel. */}
      }
      notify('Штатное скачивание недоступно. Нажмите «Скачать в браузере».');return;
    }
    const link=document.createElement('a');link.href=url;link.download=name;link.rel='noopener';document.body.append(link);link.click();link.remove();
    notify('Запрос на скачивание передан браузеру.');
  }
  async function downloadArtifact(artifact,button){
    const threadId=state.selectedThread?.id;if(!threadId||state.artifactsThreadId!==threadId)return;
    button.disabled=true;
    try{
      const ticket=await request('/threads/'+encodeURIComponent(threadId)+'/artifacts/'+encodeURIComponent(String(artifact.id||''))+'/download-ticket',{method:'POST',body:{},etag:false});
      if(state.selectedThread?.id!==threadId||state.artifactsThreadId!==threadId)return;
      const name=String(ticket.name||artifact.name||'download');downloadFile(validatedTicketUrl(ticket.url),name,button,ticket.expiresAt);
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  }
  async function loadArtifacts(){
    const threadId=state.selectedThread?.id;if(!threadId)return false;
    if(state.artifactsThreadId!==threadId)resetArtifacts(threadId);
    state.artifactsLoading=true;state.artifactsError='';renderArtifacts();$('artifact-refresh').disabled=true;
    try{
      const data=await request('/threads/'+encodeURIComponent(threadId)+'/artifacts',{etag:false,timeoutMs:25000});
      if(state.selectedThread?.id!==threadId||state.artifactsThreadId!==threadId)return false;
      if(!Array.isArray(data.artifacts))throw new Error('Сервер вернул некорректный список артефактов');
      state.artifacts=data.artifacts;state.artifactsComplete=data.complete===true;state.artifactsReason=typeof data.reason==='string'?data.reason:'';return true;
    }catch(error){if(state.selectedThread?.id===threadId&&state.artifactsThreadId===threadId)state.artifactsError=error.message;return false;}
    finally{if(state.selectedThread?.id===threadId&&state.artifactsThreadId===threadId){state.artifactsLoading=false;$('artifact-refresh').disabled=false;renderArtifacts();}}
  }
  function openArtifacts(){
    const threadId=state.selectedThread?.id;if(!threadId)return;
    dismissChatKeyboard();
    if(state.artifactsThreadId!==threadId)resetArtifacts(threadId);
    const drawer=$('artifacts-drawer');drawer.hidden=false;drawer.removeAttribute('inert');$('artifact-title').textContent='Файлы и артефакты';$('artifact-close').focus();loadArtifacts();
  }
  function closeArtifacts(){const drawer=$('artifacts-drawer');if(!drawer)return;drawer.hidden=true;drawer.setAttribute('inert','');}
  const DECISION_OPEN=new Set(['open','notification_uncertain']);
  const DECISION_TRACKED=new Set(['open','submitting','gateway_submitting','waiting_answers','pending','queued','submitted','received','received_by_active_turn','failed','uncertain','desktop_required','notification_uncertain','answered_elsewhere','answer_seen_in_chat']);
  function decisionStage(decision){return String(decision?.status||'pending').toLowerCase();}
  function decisionDraft(decision){
    const id=String(decision.id),options=Array.isArray(decision.options)?decision.options:[];
    if(!state.decisionDrafts.has(id))state.decisionDrafts.set(id,{mode:options.length?'choice':'text',choiceId:'',text:''});
    return state.decisionDrafts.get(id);
  }
  function selectedDecision(){return state.decisions.find(item=>String(item.id)===state.decisionId)||null;}
  function decisionStatusText(decision){
    const stage=decisionStage(decision);
    const gatewayLabels={gateway_submitting:'Передаём ответ в Codex',waiting_answers:'Ответ сохранён · ответьте на остальные вопросы этого запроса',accepted_by_server:'Codex принял ответ на исходный запрос',closed:'Запрос уже закрыт в Desktop'};
    if(gatewayLabels[stage])return gatewayLabels[stage];
    return ({open:'Ждёт вашего ответа',submitting:'Ответ записывается · ждём подтверждения',pending:'Ответ зарезервирован · ждём отправки в чат',queued:'Ответ поставлен в очередь · ждём подтверждения чата',submitted:'Ответ отправлен · ждём подтверждения чата',received:'Чат получил ответ',received_by_active_turn:'Ответ получен задачей · ждём обработки',completed:'Ответ обработан чатом',resolved:'Ответ обработан чатом',answered:'Ответ зарегистрирован',answered_elsewhere:'Ответ найден в исходном чате. Что его доставила панель, не подтверждено.',answer_seen_in_chat:'Ответ появился в исходном чате; способ доставки не подтверждён.',failed:'Desktop подтвердил сбой доставки',uncertain:'Проверяем доставку · не отправляйте повторно',desktop_required:'Ответьте в Codex Desktop: этот запрос требует подтверждения в приложении.',notification_uncertain:'Доставка уведомления в Telegram не подтверждена. Вы можете ответить здесь.'})[stage]||'Статус ответа уточняется';
  }
  function renderDecisionBar(){
    const bar=$('thread-decisions-bar');if(!bar)return;
    const current=state.selectedThread?.id===state.decisionsThreadId?state.decisions:[];
    const unresolved=current.filter(item=>DECISION_TRACKED.has(decisionStage(item)));
    const needsAnswer=unresolved.filter(item=>DECISION_OPEN.has(decisionStage(item))).length;
    const desktopRequired=unresolved.some(item=>['desktop_required','notification_uncertain'].includes(decisionStage(item)));
    const failed=unresolved.some(item=>decisionStage(item)==='failed'),uncertain=unresolved.some(item=>decisionStage(item)==='uncertain'),answerInChat=unresolved.some(item=>['answered_elsewhere','answer_seen_in_chat'].includes(decisionStage(item))),receivedByTurn=unresolved.some(item=>decisionStage(item)==='received_by_active_turn');
    const allReceived=unresolved.length>0&&unresolved.every(item=>decisionStage(item)==='received');
    bar.hidden=!unresolved.length;
    if(!unresolved.length){$('thread-menu-decisions').textContent='Решения';return;}
    $('thread-decisions-count').textContent=needsAnswer?needsAnswer===1?'Нужно ваше решение':`Нужны решения · ${needsAnswer}`:desktopRequired?'Нужно решение в Desktop':failed?'Сбой доставки':uncertain?'Проверяем доставку':receivedByTurn?'Ответ получен задачей':answerInChat?'Ответ найден в чате':allReceived?'Чат получил ответ':'Ответ передаётся в чат';
    $('thread-decisions-open').textContent=needsAnswer||desktopRequired?'Открыть':'Проверить';
    $('thread-menu-decisions').textContent=needsAnswer?`Решения · ${needsAnswer}`:'Решения';
  }
  function renderDecisionSubmit(){
    const decision=selectedDecision(),submit=$('decision-submit'),refresh=$('decision-refresh'),stateLine=$('decision-state');
    if(!decision){submit.hidden=true;refresh.hidden=true;stateLine.textContent=state.decisionError||'';stateLine.classList.toggle('error',Boolean(state.decisionError));return;}
    const stage=decisionStage(decision),answerable=DECISION_OPEN.has(stage),draft=decisionDraft(decision);
    const tooLong=draft.mode==='text'&&draft.text.length>3500;
    const valid=draft.mode==='choice'?Boolean(draft.choiceId):Boolean(draft.text.trim())&&!tooLong;
    submit.hidden=!answerable;submit.disabled=!valid||state.decisionSubmitting||Boolean(state.decisionError);submit.textContent=state.decisionSubmitting?'Отправляем…':'Отправить ответ';
    refresh.hidden=answerable&&!state.decisionError;refresh.disabled=state.decisionLoading||state.decisionSubmitting;
    const message=tooLong?'Ответ длиннее 3500 символов. Сократите текст.':state.decisionError||decisionStatusText(decision);
    stateLine.textContent=message;stateLine.classList.toggle('error',tooLong||Boolean(state.decisionError)||['failed','uncertain','notification_uncertain','desktop_required'].includes(stage));
  }
  function renderDecisionTabs(){
    const tabs=$('decision-tabs'),left=tabs.scrollLeft;tabs.replaceChildren();
    const items=state.decisions,decision=selectedDecision();
    tabs.hidden=items.length<=1;
    if(items.length>1)for(const [index,item] of items.entries()){const label=String(item.question||item.title||'Решение').trim();const button=el('button','decision-tab',`${index+1}. ${label}`);button.type='button';button.title=label;button.setAttribute('aria-pressed',String(item===decision));button.addEventListener('click',()=>{state.decisionId=String(item.id);state.decisionError='';renderDecisionSheet();});tabs.append(button);}
    tabs.scrollLeft=left;
  }
  function refreshDecisionSheet(changed){
    if($('decision-backdrop').hidden)return;
    const active=document.activeElement,decision=selectedDecision(),options=Array.isArray(decision?.options)?decision.options:[];
    const editing=active?.id==='decision-custom-text'&&decision&&active.dataset.decisionId===String(decision.id)&&active.dataset.optionsSignature===JSON.stringify(options)&&DECISION_OPEN.has(decisionStage(decision));
    if(editing){
      const content=$('decision-content'),scroll=document.querySelector('.decision-scroll'),top=scroll.scrollTop;
      renderDecisionTabs();$('decision-context').textContent=state.selectedThread?plainTitle(state.selectedThread,'Чат'):'';
      const title=String(decision.title||''),heading=content.querySelector('.decision-title'),question=content.querySelector('.decision-question');
      if(title){if(heading)heading.textContent=title;else{const newHeading=el('h3','decision-title',title);content.insertBefore(newHeading,question);}}else heading?.remove();
      if(question)question.textContent=String(decision.question??'');
      renderDecisionSubmit();scroll.scrollTop=top;
      const overlap=active.getBoundingClientRect().bottom-scroll.getBoundingClientRect().bottom+8;
      if(overlap>0)scroll.scrollTop+=overlap;
      return;
    }
    if(changed||!decision)renderDecisionSheet();else renderDecisionSubmit();
  }
  function renderDecisionSheet(){
    const content=$('decision-content');renderDecisionTabs();content.replaceChildren();
    const decision=selectedDecision();
    $('decision-context').textContent=state.selectedThread?plainTitle(state.selectedThread,'Чат'):'';
    if(!decision){content.append(el('p','decision-empty',state.decisionLoading?'Получаем запросы этого чата…':state.decisionError?'Не удалось получить запросы. Проверьте соединение и повторите.':'В этом чате пока нет запросов на решение.'));renderDecisionSubmit();return;}
    const title=String(decision.title||'');if(title)content.append(el('h3','decision-title',title));
    const question=el('p','decision-question',String(decision.question??''));content.append(question);
    if(decision.details){const details=el('details','decision-details'),summary=el('summary','','Подробности запроса'),body=el('pre','',JSON.stringify(decision.details,null,2));details.append(summary,body);content.append(details);}
    const stage=decisionStage(decision),answerable=DECISION_OPEN.has(stage),draft=decisionDraft(decision),options=Array.isArray(decision.options)?decision.options:[];
    if(answerable){
      if(decision.customAnswerAllowed===false)draft.mode='choice';
      if(options.length){const modes=el('div','decision-modes');modes.setAttribute('role','group');modes.setAttribute('aria-label','Способ ответа');for(const [mode,label] of (decision.customAnswerAllowed===false?[['choice','Выбрать решение']]:[['choice','Выбрать вариант'],['text','Свой ответ']])){const button=el('button','decision-mode'+(draft.mode===mode?' selected':''),label);button.type='button';button.setAttribute('aria-pressed',String(draft.mode===mode));button.addEventListener('click',()=>{draft.mode=mode;renderDecisionSheet();});modes.append(button);}content.append(modes);}
      if(draft.mode==='choice'&&options.length){const group=el('div','decision-options');group.setAttribute('role','group');group.setAttribute('aria-label','Предложенные варианты');for(const option of options){const choiceId=String(option.id),button=el('button','decision-option'+(draft.choiceId===choiceId?' selected':''));button.type='button';button.setAttribute('aria-pressed',String(draft.choiceId===choiceId));button.append(el('span','decision-option-mark',draft.choiceId===choiceId?'✓':''));const copy=el('span','decision-option-copy');copy.append(el('strong','',String(option.label??'')));if(option.description!==undefined&&option.description!==null)copy.append(el('span','',String(option.description)));button.append(copy);button.addEventListener('click',()=>{draft.choiceId=choiceId;renderDecisionSheet();});group.append(button);}content.append(group);}
      else{const heading=el('div','decision-custom-heading'),label=el('label','decision-custom-label','Ваш ответ');label.htmlFor='decision-custom-text';const input=el('textarea','decision-custom-text');input.id='decision-custom-text';input.dataset.decisionId=String(decision.id);input.dataset.optionsSignature=JSON.stringify(options);input.rows=5;input.maxLength=3500;input.placeholder='Напишите свой вариант решения или уточнение…';input.value=draft.text;const counter=el('small','decision-counter');const updateCounter=()=>{counter.textContent='Осталось '+Math.max(0,3500-input.value.length)+' из 3500 символов';counter.classList.toggle('near-limit',input.value.length>=3200);};input.addEventListener('input',()=>{draft.text=input.value;updateCounter();renderDecisionSubmit();});updateCounter();heading.append(label,counter);content.append(heading,input);}
      content.append(el('p','decision-guidance','Ответ будет направлен именно в этот чат. До подтверждения получения повторно отправлять его не нужно.'));
    }else{
      const answer=decision.answer;
      const answerText=typeof answer==='string'?answer:answer&&typeof answer==='object'?answer.text??answer.label??answer.choiceLabel:null;
      if(answerText!==null&&answerText!==undefined)content.append(el('div','decision-answer','Ваш ответ: '+String(answerText)));
    }
    renderDecisionSubmit();
  }
  function closeDecisions(){
    const backdrop=$('decision-backdrop');if(backdrop.hidden)return;
    backdrop.hidden=true;backdrop.setAttribute('inert','');state.decisionError='';
    if(decisionReturnFocus?.isConnected)decisionReturnFocus.focus({preventScroll:true});decisionReturnFocus=null;
  }
  function openDecisions(id='',refreshIfNeeded=true){
    if(!state.selectedThread)return;
    dismissChatKeyboard();closeThreadMenu();closeArtifacts();setProgressExpanded(false);
    if(id)state.decisionId=String(id);
    if(!selectedDecision())state.decisionId=String(state.decisions.find(item=>DECISION_OPEN.has(decisionStage(item)))?.id||state.decisions[0]?.id||'');
    decisionReturnFocus=document.activeElement;
    const backdrop=$('decision-backdrop');backdrop.hidden=false;backdrop.removeAttribute('inert');renderDecisionSheet();$('decision-close').focus({preventScroll:true});
    if(refreshIfNeeded&&(state.decisionsThreadId!==state.selectedThread.id||state.decisionError))void loadDecisions(state.selectedThread.id);
  }
  async function submitDecision(){
    const decision=selectedDecision(),threadId=state.selectedThread?.id;if(!decision||!threadId||state.decisionSubmitting||!DECISION_OPEN.has(decisionStage(decision)))return;
    const draft=decisionDraft(decision),body=draft.mode==='choice'?{choiceId:draft.choiceId}:{text:draft.text};
    if(draft.mode==='choice'?!draft.choiceId:!draft.text.trim()||draft.text.length>3500)return;
    state.decisionSubmitting=true;state.decisionError='';renderDecisionSubmit();
    try{
      const data=await request('/decisions/'+encodeURIComponent(String(decision.id))+'/answer',{method:'POST',body,etag:false,timeoutMs:25000});
      if(state.selectedThread?.id!==threadId)return;
      if(!data.decision||String(data.decision.id)!==String(decision.id))throw new Error('Подтверждение ответа не получено. Проверьте доставку перед повтором.');
      const index=state.decisions.findIndex(item=>String(item.id)===String(decision.id));if(index>=0)state.decisions[index]=data.decision;
      renderDecisionBar();renderDecisionSheet();
      notify('Ответ принят панелью. Статус доставки показан в решении.');
    }catch(error){
      if(state.selectedThread?.id!==threadId)return;
      state.decisionError=error.message+' Проверьте доставку перед повтором.';
      renderDecisionSubmit();
    }finally{state.decisionSubmitting=false;if(state.selectedThread?.id===threadId)renderDecisionSubmit();}
  }
  async function loadDecisions(threadId=state.selectedThread?.id){
    if(!threadId)return false;
    if(decisionRequest?.threadId===threadId)return decisionRequest.promise;
    const controller=new AbortController(),promise=(async()=>{
      if(state.selectedThread?.id===threadId){state.decisionLoading=true;renderDecisionSubmit();}
      try{
        const data=await request('/decisions?thread='+encodeURIComponent(threadId),{etag:false,timeoutMs:10000,signal:controller.signal});
        if(controller.signal.aborted||state.selectedThread?.id!==threadId)return false;
        if(!Array.isArray(data.decisions))throw new Error('Список решений недоступен');
        const next=data.decisions.filter(item=>item&&String(item.threadId)===threadId&&item.id!==undefined);
        const changed=state.decisionsThreadId!==threadId||JSON.stringify(state.decisions)!==JSON.stringify(next);
        state.decisionsThreadId=threadId;state.decisions=next;state.decisionError='';
        if(!selectedDecision())state.decisionId=String(next.find(item=>DECISION_OPEN.has(decisionStage(item)))?.id||next[0]?.id||'');
        renderDecisionBar();refreshDecisionSheet(changed);
        if(deepLinkPending&&deepLink?.thread===threadId){deepLinkPending=false;openDecisions(deepLink.decision);}
        return true;
      }catch(error){if(controller.signal.aborted||state.selectedThread?.id!==threadId)return false;state.decisionError=error.message;if(deepLinkPending&&deepLink?.thread===threadId){deepLinkPending=false;openDecisions(deepLink.decision,false);}else refreshDecisionSheet(false);return false;}
      finally{if(state.selectedThread?.id===threadId){state.decisionLoading=false;renderDecisionSubmit();}if(decisionRequest?.controller===controller)decisionRequest=null;}
    })();decisionRequest={threadId,controller,promise};return promise;
  }
  function scheduleDecisionWatch(){
    clearTimeout(decisionWatchTimer);decisionWatchTimer=0;
    if(document.hidden||state.view!=='thread'||!state.selectedThread)return;
    const threadId=state.selectedThread.id;
    decisionWatchTimer=setTimeout(async()=>{
      decisionWatchTimer=0;if(document.hidden||state.view!=='thread'||state.selectedThread?.id!==threadId)return;
      await loadDecisions(threadId);scheduleDecisionWatch();
    },20000);
  }
  function kind(thread) {return String(thread.kind||thread.source||'').toLowerCase().includes('chatgpt')?'ChatGPT':'Codex';}
  function projectSurface(project){return String(project.kind||project.projectKind||project.source||'').toLowerCase().includes('chatgpt')?'chatgpt':'codex';}
  function inSurface(thread){return kind(thread).toLowerCase()===state.surface;}
  function renderSurface(){
    const name=state.surface==='chatgpt'?'ChatGPT':'Codex';
    document.querySelectorAll('[data-surface]').forEach(button=>{const selected=button.dataset.surface===state.surface;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});
    $('chats-title').textContent='Чаты '+name;$('chats-description').textContent='Раздел '+name;
    $('projects-title').textContent='Проекты '+name;$('projects-description').textContent=state.surface==='chatgpt'?'Выберите проект ChatGPT Work для новой задачи':'Выберите локальный проект Codex для новой задачи';
    $('archived-title').textContent='Архив '+name;$('archived-description').textContent='Архивные чаты '+name;
    document.title='Рабочий стол · '+name;
  }
  function switchSurface(surface){
    if(surface!== 'chatgpt'&&surface!=='codex')return;
    state.surface=surface;try{localStorage.setItem('miniapp-surface',surface);}catch{}
    state.filter='all';state.search='';$('chat-search').value='';document.querySelectorAll('[data-chat-filter]').forEach(button=>button.classList.toggle('selected',button.dataset.chatFilter==='all'));
    renderSurface();renderThreads();renderProjects();renderArchived();renderModelNote();show('chats',{quiet:true});
  }
  function status(thread) {const s=String(thread.status||'').toLowerCase();return ['active','running','inprogress','in_progress','processing'].includes(s)?'В работе':['failed','error'].includes(s)?'Ошибка':['complete','completed','done'].includes(s)?'Завершено':['unknown','notloaded',''].includes(s)?'Статус не загружен':'Ожидает';}
  function active(thread) {return status(thread)==='В работе';}
  function toDate(value){if(value===null||value===undefined||value==='')return new Date(NaN);const number=typeof value==='number'?value:typeof value==='string'&&/^\d+(?:\.\d+)?$/.test(value)?Number(value):NaN;return Number.isFinite(number)?new Date(number<1e11?number*1000:number):new Date(value);}
  function updated(thread){const date=toDate(thread.updatedAt||thread.updated_at);return Number.isNaN(date.getTime())?'':new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short'}).format(date);}
  function mskTime(milliseconds){return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(milliseconds));}
  function planLabel(thread){
    if(!active(thread))return '';
    const plan=thread.plan;
    if(!plan||!Number.isFinite(plan.earliest)||!Number.isFinite(plan.latest))return '';
    if(Date.now()>plan.latest*1000)return 'План до '+mskTime(plan.latest*1000)+' МСК истёк · нужна новая оценка';
    return 'План: '+mskTime(plan.earliest*1000)+'–'+mskTime(plan.latest*1000)+' МСК';
  }
  function renderThreadPlan(thread){const banner=$('thread-plan'),label=planLabel(thread);banner.hidden=!label;banner.textContent=label;}
  function byRecent(a,b){return toDate(b.updatedAt||b.updated_at)-toDate(a.updatedAt||a.updated_at);}
  function plainTitle(item,fallback){return String(item.title||item.name||fallback).slice(0,150);}
  function newOperationId(){const bytes=new Uint8Array(16);if(globalThis.crypto?.getRandomValues)globalThis.crypto.getRandomValues(bytes);else for(let i=0;i<bytes.length;i++)bytes[i]=Math.floor(Math.random()*256);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;}
  const DELIVERY_STAGES=new Set(['queued','received','completed','failed','uncertain']);
  function saveOperations(){try{sessionStorage.setItem('miniapp-operations',JSON.stringify(state.operations.map(({id,kind,targetId,at,ok,deliveryStage,historyReceived})=>({id,kind,targetId,at,ok,deliveryStage,historyReceived}))))}catch{}}
  function renderOperations(){
    const panel=$('operations-section'),list=$('operations-list');panel.hidden=!state.operations.length;
    list.replaceChildren(...state.operations.map(op=>{const delayed=op.kind==='followup'&&op.deliveryStage!=='completed'&&op.deliveryStage!=='failed'&&Date.now()-op.at>120000;const done=op.kind==='followup'?op.deliveryStage==='completed':op.ok===true;const failed=op.deliveryStage==='failed'||!op.deliveryStage&&op.ok===false;const card=el('article','operation-card'+(done?' complete':failed?' failed':''));const labels={new_task:'Новая задача',followup:'Уточнение в чате',view_more:'Загрузка истории',thread_rename:'Название чата',thread_pin:'Закрепление чата',thread_unpin:'Открепление чата',thread_archive:'Архивация чата',thread_restore:'Восстановление чата'};const label=labels[op.kind]||'Действие с чатом';const status=op.kind==='followup'?(failed?'Не выполнено':done?'Завершено в чате':op.deliveryStage==='received'?'Принято чатом · ожидаем результат':op.historyReceived?'Текст найден в истории · ожидаем результат':op.deliveryStage==='uncertain'?'Доставка не подтверждена':delayed?'Ожидание Desktop дольше обычного':op.ok===true?'Передано в Desktop · ждём подтверждения':'В очереди · ожидаем Desktop'):(op.ok===true?'Подтверждено Desktop':op.ok===false?'Не выполнено':'Принято · ожидает подтверждения');const detail=op.kind==='followup'?(failed?op.text||'Desktop сообщил об ошибке.':done?'В чате получен итоговый ответ.':op.deliveryStage==='uncertain'?'Состояние неизвестно; повторная отправка может создать дубль.':op.deliveryStage==='received'?'Сообщение появилось в чате; задача ещё выполняется.':op.historyReceived?'Совпадающее сообщение есть в истории; завершение ещё не подтверждено.':delayed?'Отправка ещё не подтверждена чатом; проверяем автоматически.':'Запрос принят компьютером; проверяем его в чате.'):(op.text||'Desktop ещё не подтвердил действие.');card.append(el('strong','',label+' · '+status),el('small','',detail));if(!failed&&!done&&op.kind==='new_task')card.append(el('small','operation-plan','Предварительный ориентир: '+mskTime(op.at+20*60000)+'–'+mskTime(op.at+45*60000)+' МСК'));return card;}));
    const recent=state.operations.find(op=>op.kind==='followup'&&op.targetId===state.selectedThread?.id),inline=$('thread-operation-status');inline.hidden=!recent;inline.classList.toggle('failed',recent?.deliveryStage==='failed'||!recent?.deliveryStage&&recent?.ok===false);if(recent)inline.textContent=recent.deliveryStage==='failed'||!recent.deliveryStage&&recent.ok===false?'Уточнение не отправлено: '+(recent.text||'ошибка'):recent.deliveryStage==='completed'?'Задача завершила обработку уточнения':recent.deliveryStage==='received'?'Уточнение принято чатом · ожидаем ответ':recent.historyReceived?'Текст найден в истории чата · ожидаем ответ':recent.deliveryStage==='uncertain'?'Доставка не подтверждена · не повторяйте без проверки':recent.ok===true?'Передано в Desktop · ждём подтверждения чата':'Уточнение в очереди · ожидает Desktop';
  }
  function trackOperation(id,kind,targetId){const exact=String(id);if(!/^\d{19}$/.test(exact))throw new Error('Сервер не вернул точный номер операции');state.operations=state.operations.filter(op=>op.id!==exact);state.operations.unshift({id:exact,kind,targetId,at:Date.now(),ok:null,text:''});state.operations=state.operations.slice(0,8);saveOperations();renderOperations();}
  async function pollOperations(){
    let changed=false;
    await Promise.allSettled(state.operations.filter(op=>op.kind==='followup'&&state.view==='thread'?op.targetId===state.selectedThread?.id&&(!['completed','failed'].includes(op.deliveryStage)||state.threadMessages.some(row=>row.queued&&row.operationId===op.id)):op.ok===null).map(async op=>{
      const result=await request('/operations/'+op.id,{timeoutMs:8000});
      const stage=DELIVERY_STAGES.has(result.deliveryStage)?result.deliveryStage:'';
      if(typeof result.ok!=='boolean'&&!stage)return;
      const previousStage=op.deliveryStage,previousOk=op.ok,previousText=op.text;
      if(typeof result.ok==='boolean')op.ok=result.ok;
      if(op.kind==='followup')op.deliveryStage=stage;
      if(stage==='failed')op.ok=false;
      if(stage==='completed')op.ok=true;
      if(['queued','received','uncertain'].includes(stage)&&op.ok===false)op.ok=null;
      op.text=String(result.text||'').slice(0,280);
      const opChanged=op.deliveryStage!==previousStage||op.ok!==previousOk||op.text!==previousText;
      changed ||= opChanged;
      if(opChanged&&op.kind==='followup'&&op.targetId===state.selectedThread?.id)renderTimeline();
      if(op.ok===true){
        if(op.kind==='view_more'||op.kind==='followup'&&stage!==previousStage)lastFetch.detail=0;
        if(op.kind==='new_task'||op.kind.startsWith('thread_')){lastFetch.threads=0;lastFetch.archived=0;}
      }else if(op.ok===false&&op.kind==='followup'&&op.targetId===state.selectedThread?.id){
        state.threadMessages.filter(row=>row.operationId===op.id).forEach(row=>row.delivery='failed');renderTimeline();notify(op.text||'Уточнение не отправлено',true);
      }
      if(op.kind==='view_more'&&op.targetId===state.selectedThread?.id){state.historyPending=false;if(result.ok===false){state.historyOperationId='';notify(op.text||'Не удалось загрузить историю',true);}renderHistoryButton();}
    }));
    const newlyDelayed=state.operations.some(op=>{if(op.kind==='followup'&&!['completed','failed'].includes(op.deliveryStage)&&Date.now()-op.at>120000&&!op.delayedShown){op.delayedShown=true;return true;}return false;});
    if(changed)saveOperations();
    if(changed||newlyDelayed)renderOperations();
  }
  function visibleDeliveryPending(){
    const id=state.selectedThread?.id;
    return Boolean(id&&state.view==='thread'&&!document.hidden&&(
      state.operations.some(op=>op.kind==='followup'&&op.targetId===id&&(op.deliveryStage?!['completed','failed'].includes(op.deliveryStage):op.ok===null))||
      state.threadMessages.some(row=>row.queued&&row.delivery!=='failed')
    ));
  }
  function scheduleDeliveryWatch(){
    clearTimeout(deliveryWatchTimer);deliveryWatchTimer=0;
    if(!visibleDeliveryPending()){deliveryWatchThread='';deliveryWatchSince=0;return;}
    const id=state.selectedThread.id;
    if(deliveryWatchThread!==id){deliveryWatchThread=id;deliveryWatchSince=Date.now();}
    const age=Date.now()-deliveryWatchSince;
    const delay=age<30000?2000:age<120000?5000:age<600000?15000:60000;
    deliveryWatchTimer=setTimeout(()=>{
      deliveryWatchTimer=0;
      if(!visibleDeliveryPending()||state.selectedThread?.id!==id){scheduleDeliveryWatch();return;}
      if(state.refreshing){scheduleDeliveryWatch();return;}
      // Check the operation and the selected chat's history together. A queue
      // acknowledgement alone does not confirm that the message reached the chat.
      void refresh(true,true);
    },delay);
  }
  function show(view, options={}) {
    if(view!=='kanban'&&kanban?.pending()){notify('Дождитесь результата сохранения карточки Kanban.');return;}
    closeDrawer();
    if(view!=='kanban')kanban?.deactivate();
    if(view!=='thread')closeDecisions();
    if(view!=='thread'&&threadDetailRequest){threadSelectionVersion++;threadDetailRequest.abort();threadDetailRequest=null;}
    state.previous=state.view; state.view=view;
    document.body.classList.toggle('thread-open',view==='thread');
    document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===view+'-view'));
    document.querySelectorAll('.nav-item').forEach(v=>v.classList.toggle('active',v.dataset.view===view));
    $('mode-chats').classList.toggle('selected',view==='chats'&&state.filter!=='active');
    $('mode-work').classList.toggle('selected',view==='home'||view==='chats'&&state.filter==='active');
    if(view==='thread'||view==='kanban') tg?.BackButton?.show?.(); else tg?.BackButton?.hide?.();
    if(view!=='thread') document.querySelector('.workspace').scrollTop=0;
    if(view==='chats' && !options.quiet&&(!state.threads.length||Date.now()-lastFetch.threads>=20000)) loadThreads();
    if(view==='projects' && !options.quiet&&(!state.projects.length||Date.now()-lastFetch.projects>=60000)) loadProjects();
    if(view==='archived' && !options.quiet) loadArchived();
    if(view==='kanban')kanban?.activate();
    if(view==='new') {renderProjectOptions();renderModelNote();if(!state.projects.length) loadProjects();}
    syncTaskProgress();
    updateChatViewport();
    scheduleDeliveryWatch();
    scheduleDecisionWatch();
  }
  function makeThreadCard(thread) {
    const button=el('button','thread-card');button.type='button';button.dataset.threadId=thread.id;
    const avatar=el('span','thread-avatar'+(kind(thread)==='ChatGPT'?' chatgpt':''),kind(thread)==='ChatGPT'?'✦':'⌘');
    const copy=el('span','card-copy');const title=el('span','card-title');title.append(el('span','',plainTitle(thread,'Без названия')));
    const meta=el('span','card-meta');meta.append(el('span','',kind(thread)),el('span','meta-separator','·'),el('span','',status(thread)));
    copy.append(title,meta);if(active(thread))copy.append(el('span','thread-plan-line',planLabel(thread)));const right=el('span','card-right');right.append(el('span','',updated(thread)),el('span','card-chevron','›'));
    button.append(avatar,copy,right);button.addEventListener('click',()=>openThread(thread.id));return button;
  }
  function renderThreads() {
    const ordered=state.threads.filter(inSurface).sort((a,b)=>Number(Boolean(b.pinned))-Number(Boolean(a.pinned))||byRecent(a,b));$('thread-count').textContent=ordered.length;$('chat-total').textContent=ordered.length;
    $('active-count').textContent=ordered.filter(active).length;
    const recent=$('recent-list');recent.replaceChildren(...(ordered.length?ordered.slice(0,3).map(makeThreadCard):[el('div','empty-mini','Чатов пока нет')]));
    const filtered=ordered.filter(t=>(state.filter==='all'||state.filter==='active'&&active(t))&&plainTitle(t,'').toLowerCase().includes(state.search));
    const list=$('thread-list');list.replaceChildren(...(filtered.length?filtered.map(makeThreadCard):[el('div','empty-state',state.search?'Ничего не найдено':'Здесь пока нет чатов')]));
    $('load-more').hidden=!state.nextCursor;
    const drawer=$('drawer-recent');drawer.replaceChildren(...(ordered.length?ordered.slice(0,12).map(t=>{const button=el('button','drawer-thread');button.type='button';button.append(el('span','drawer-thread-icon',kind(t)==='ChatGPT'?'✦':'⌘'),el('span','drawer-thread-title',plainTitle(t,'Без названия')));button.addEventListener('click',()=>openThread(t.id));return button;}):[el('div','drawer-empty','Чатов пока нет')]));
  }
  function renderProjects() {
    const projects=state.projects.filter(project=>projectSurface(project)===state.surface);$('project-count').textContent=projects.length;const list=$('project-list');
    const nodes=projects.map(project=>{
      const button=el('button','project-card');button.type='button';
      const avatar=el('span','project-avatar','◇');const copy=el('span','card-copy');copy.append(el('span','card-title',plainTitle(project,'Проект')),el('span','card-meta',project.isGitRepository?'Codex · Git-проект':String(project.kind||project.source||'Проект')));
      button.append(avatar,copy,el('span','card-chevron','›'));button.addEventListener('click',()=>{show('new',{quiet:true});$('project-select').value=project.projectId||project.id;renderModelNote();});return button;
    }); list.replaceChildren(...(nodes.length?nodes:[el('div','empty-state',state.surface==='chatgpt'?'Проекты ChatGPT Work пока недоступны':'Проекты Codex не найдены')]));renderProjectOptions();
  }
  function renderArchived() {
    const list=$('archived-list');
    const nodes=state.archived.filter(inSurface).map(thread=>{const card=el('div','archive-card');const copy=el('div','archive-copy');copy.append(el('strong','',plainTitle(thread,'Без названия')),el('small','',kind(thread)+' · архив'));card.append(copy);if(kind(thread)==='Codex'){const restore=el('button','archive-restore','Восстановить');restore.type='button';restore.addEventListener('click',()=>threadAction('restore','',thread));card.append(restore);}else card.append(el('small','archive-unavailable','Восстановление недоступно'));return card;});
    list.replaceChildren(...(nodes.length?nodes:[el('div','empty-state','В архиве пока нет чатов')]));
  }
  function renderProjectOptions() {
    const select=$('project-select');const previous=select.value;select.replaceChildren(new Option('Выберите проект',''));
    for(const project of state.projects.filter(project=>projectSurface(project)===state.surface)) select.add(new Option(plainTitle(project,'Проект'),project.projectId||project.id));
    if([...select.options].some(option=>option.value===previous))select.value=previous;
  }
  function renderModelNote() {
    const project=state.projects.find(p=>(p.projectId||p.id)===$('project-select').value);
    const isChatGPT=state.surface==='chatgpt'||project&&projectSurface(project)==='chatgpt';
    $('new-model-controls').hidden=Boolean(isChatGPT);
    $('new-model-note').textContent=isChatGPT?'Для ChatGPT Work модель через Desktop не задаётся.':'Выбор модели и интенсивности применяется к этой задаче Codex.';
  }
  function renderThreadModelControls(){
    const isChatGPT=state.selectedThread&&kind(state.selectedThread)==='ChatGPT';
    $('composer-settings-toggle').hidden=Boolean(isChatGPT);
    $('thread-model-controls').hidden=Boolean(isChatGPT)||$('composer-settings-toggle').getAttribute('aria-expanded')!=='true';
    document.querySelector('.composer-hint').textContent=isChatGPT?'Запрос появится в чате после подтверждения Desktop.':'Запрос появится в чате после подтверждения Desktop · модель выбрана выше.';
  }
  async function loadThreads(more=false) {
    try {
      const url='/threads'+(more&&state.nextCursor?'?cursor='+encodeURIComponent(state.nextCursor):'');
      const data=await request(url);const items=Array.isArray(data)?data:(data.threads||data.items||[]);
      lastFetch.threads=Date.now();
      if(data.notModified)return true;
      state.threads=more?[...state.threads,...items.filter(t=>!state.threads.some(old=>old.id===t.id))]:items;
      if(state.selectedThread){const latest=state.threads.find(t=>t.id===state.selectedThread.id);if(latest){state.selectedThread=latest;renderThreadPlan(latest);$('thread-status').textContent=status(latest);$('thread-status').classList.toggle('idle',!active(latest));}}
      state.nextCursor=data.nextCursor||data.next_cursor||null;renderThreads();
      if(deepLinkPending&&!deepLinkNavigated){deepLinkNavigated=true;void openThread(deepLink.thread);}
      return true;
    } catch(error) {connectionError(error);if(!state.threads.length){$('thread-list').replaceChildren(el('div','empty-state',error.message));$('recent-list').replaceChildren(el('div','empty-mini',error.message));}if(deepLinkPending&&!deepLinkNavigated){deepLinkNavigated=true;void openThread(deepLink.thread);}return false;}
  }
  async function loadProjects() {
    try {const data=await request('/projects');lastFetch.projects=Date.now();if(data.notModified)return true;state.projects=Array.isArray(data)?data:(data.projects||data.items||[]);renderProjects();return true;}
    catch(error){connectionError(error);if(!state.projects.length)$('project-list').replaceChildren(el('div','empty-state',error.message));return false;}
  }
  async function loadArchived(){
    try{const data=await request('/archived');lastFetch.archived=Date.now();if(data.notModified)return true;state.archived=data.threads||[];renderArchived();return true;}
    catch(error){connectionError(error);$('archived-list').replaceChildren(el('div','empty-state',error.message));return false;}
  }
  async function loadUsage(){
    try{const data=await request('/usage',{etag:false,connection:false});applyUsage(data);usageLoaded=true;renderUsage();return true;}
    catch{usageStatus.error='usage_unavailable';renderUsage();return false;}
  }
  function applyUsage(data){
    if(data.usage||!data.refreshError)state.usage=data.usage||null;
    usageStatus.stale=data.stale===true;usageStatus.refreshing=data.refreshing===true;usageStatus.error=data.refreshError||null;
    usageStatus.checkedAt=typeof data.checkedAt==='number'?data.checkedAt:null;
  }
  function refreshUsage(force=false){
    if(usagePromise)return usagePromise;
    if(!force&&lastUsageAttempt&&Date.now()-lastUsageAttempt<30000){renderUsage();return Promise.resolve(!usageStatus.error&&!usageStatus.refreshing);}
    lastUsageAttempt=Date.now();usageStatus.busy=true;renderUsage();
    usagePromise=(async()=>{
      if(!usageLoaded)await loadUsage();
      try{
        const data=await request('/usage/refresh',{method:'POST',body:{},timeoutMs:25000,connection:false});
        applyUsage(data);usageLoaded=true;lastFetch.usage=Date.now();
        usageStatus.outcome=data.refreshed===true?'updated':'cached';
        const updated=Number(state.usage?.updated_at||0);
        return !usageStatus.error&&!usageStatus.refreshing&&!usageStatus.stale&&usageWindows().length>0&&updated>0&&Date.now()-updated*1000<180000;
      }catch{usageStatus.error='usage_unavailable';return false;}
      finally{usageStatus.busy=false;renderUsage();}
    })().finally(()=>{usagePromise=null;});
    return usagePromise;
  }
  function normalizeMessage(row){
    const role=String(row.role||row.type||row.author||'').toLowerCase();
    const fromUser=role==='user'||role==='human';
    // Assistant originals are discovered independently in the Files drawer.
    const attachments=fromUser?(Array.isArray(row.attachments)?row.attachments:Array.isArray(row.files)?row.files:[]):[];
    return {id:String(row.id||row.itemId||Math.random()),fromUser,role:role==='user'?'Вы':role==='assistant'||role==='agentmessage'?'Codex / ChatGPT':role||'Сообщение',text:String(row.text||row.content||row.summary||''),at:row.createdAt||row.created_at||null,attachments,queued:Boolean(row.queued),delivery:row.delivery||'',operationId:row.operationId||null};
  }
  function renderTimeline(forceBottom=false) {
    const list=$('thread-timeline');const atBottom=forceBottom||list.scrollHeight-list.scrollTop-list.clientHeight<90;const oldTop=list.scrollTop;const listTop=list.getBoundingClientRect().top;const anchor=[...list.children].find(node=>node.getBoundingClientRect().bottom>listTop);const anchorId=anchor?.dataset.messageId;const anchorOffset=anchor?anchor.getBoundingClientRect().top-listTop:0;const rows=state.threadMessages.map(normalizeMessage).filter(m=>m.text||m.attachments.length);
    list.replaceChildren(...(rows.length?rows.map(m=>{const outer=el('article','message'+(m.fromUser?' from-user':''));outer.dataset.messageId=m.id;outer.append(el('span','message-role',m.role));if(m.text)outer.append(messageBubble(m.text));
      if(m.attachments.length){const files=el('div','message-files');for(const file of m.attachments){const button=el('button','message-file');button.type='button';const mime=String(file.mimeType||file.mime_type||'');button.append(el('span','file-icon',mime.startsWith('image/')?'▧':mime.startsWith('video/')?'▷':'▤'),el('span','',String(file.name||'Вложение')));button.addEventListener('click',()=>downloadAttachment(file).catch(error=>notify(error.message,true)));files.append(button);}outer.append(files);}
      if(m.queued){const operation=state.operations.find(op=>op.id===m.operationId);const delivery=operation?.deliveryStage||m.delivery||(operation?.ok===true?'sent':operation?.ok===false?'failed':'pending');outer.append(el('span','message-time',delivery==='completed'?'Обработка завершена · обновляем историю':delivery==='received'?'Принято чатом · ожидаем ответ':delivery==='uncertain'?'Доставка не подтверждена · проверьте статус':delivery==='sent'?'Передано в Desktop · ожидаем запись в истории':delivery==='failed'?'Не отправлено · проверьте статус действия':'Принято на компьютере · ожидает Desktop'));}
      else if(m.at){const date=toDate(m.at);if(!Number.isNaN(date.getTime()))outer.append(el('span','message-time',new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit'}).format(date)));}return outer;}):[el('div','empty-state','Сообщений для просмотра пока нет')]));
    if(atBottom)list.scrollTop=list.scrollHeight;
    else{const restored=[...list.children].find(node=>node.dataset.messageId===anchorId);list.scrollTop=restored?oldTop+restored.getBoundingClientRect().top-listTop-anchorOffset:oldTop;}
    scheduleDeliveryWatch();
  }
  function renderHistoryButton(){const button=$('thread-load-older');button.hidden=!state.historyCursor;button.disabled=state.historyPending;button.textContent=state.historyPending?'Загружаем предыдущие сообщения…':'Показать предыдущие сообщения ↑';}
  function resetMessageOperation(){state.messageOperationId='';state.messageAttachmentIds=null;}
  function resetTaskOperation(){state.taskOperationId='';state.taskAttachmentIds=null;}
  function pendingMessagesNotIn(received){
    const operations=new Set(received.map(row=>String(row.operationId||''))),ids=new Set(received.map(row=>String(row.id)));
    const confirmed=received.filter(row=>!row.queued&&['user','human'].includes(String(row.role||row.type||row.author||'').toLowerCase()));
    const matched=new Set();
    return state.threadMessages.filter(row=>{
      if(!row.queued||ids.has(String(row.id))||operations.has(String(row.operationId||'')))return false;
      const op=state.operations.find(item=>item.id===row.operationId);
      if(!op||op.deliveryStage==='failed'||!op.deliveryStage&&op.ok===false)return false;
      // A native user message often has no operationId. Match one exact
      // message after receipt proof, keeping repeated equal texts distinct.
      if(op.ok===true||['received','completed'].includes(op.deliveryStage)){
        const sentAt=toDate(row.createdAt||row.created_at).getTime();
        const index=confirmed.findIndex((item,i)=>!matched.has(i)&&item.text===row.text&&Number.isFinite(sentAt)&&toDate(item.createdAt||item.created_at).getTime()>=sentAt-10000);
        if(index>=0){matched.add(index);if(!op.historyReceived){op.historyReceived=true;saveOperations();renderOperations();}return false;}
      }
      return true;
    });
  }
  async function openThread(id) {
    const thread=state.threads.find(t=>t.id===id)||(deepLink?.thread===id?{id,kind:'codex',title:'Чат',status:''}:null);if(!thread)return;
    threadDetailRequest?.abort();const controller=new AbortController();threadDetailRequest=controller;const selection=++threadSelectionVersion;
    decisionRequest?.controller.abort();decisionRequest=null;closeDecisions();state.decisions=[];state.decisionsThreadId='';state.decisionId='';state.decisionError='';renderDecisionBar();
    state.threadMessages=[];
    setProgressExpanded(false);
    state.selectedThread=thread;closeArtifacts();resetArtifacts(id);renderThreadPlan(thread);state.historyCursor=null;state.historyPending=false;state.historyOperationId='';state.historyExpanded=false;renderHistoryButton();resetMessageOperation();renderOperations();renderThreadModelControls();$('file-path').value='';$('file-import-form').hidden=true;$('file-import-toggle').setAttribute('aria-expanded','false');$('thread-title').textContent=plainTitle(thread,'Чат');$('thread-kind').textContent=kind(thread).toUpperCase();$('thread-pin').textContent=thread.pinned?'Открепить чат':'Закрепить чат';$('thread-actions').hidden=true;$('thread-actions-toggle').setAttribute('aria-expanded','false');$('thread-rename-form').hidden=true;$('thread-archive').dataset.confirm='';$('thread-archive').textContent='Архивировать чат';
    $('thread-status').textContent=status(thread);$('thread-status').classList.toggle('idle',!active(thread));$('thread-timeline').replaceChildren(el('div','empty-state','Загружаем переписку…'));show('thread',{quiet:true});void loadDecisions(id);
    try{const data=await request('/threads/'+encodeURIComponent(id),{etag:false,timeoutMs:25000,signal:controller.signal});if(controller.signal.aborted||selection!==threadSelectionVersion||state.selectedThread?.id!==id||state.view!=='thread')return;lastFetch.detail=Date.now();state.selectedThread=data.thread||thread;renderThreadPlan(state.selectedThread);renderThreadModelControls();const received=data.messages||data.turns||[];state.threadMessages=[...received,...pendingMessagesNotIn(received)];state.historyCursor=data.nextCursor||null;renderHistoryButton();renderTimeline(true);}
    catch(error){if(controller.signal.aborted||selection!==threadSelectionVersion||state.selectedThread?.id!==id)return;$('thread-timeline').replaceChildren(el('div','empty-state',error.message));notify(error.message,true);}
    finally{if(threadDetailRequest===controller)threadDetailRequest=null;}
  }
  function renderFiles(files,target){const box=$(target);box.hidden=!files.length;box.replaceChildren(...files.map((file,index)=>{const pill=el('span','attachment-pill');pill.append(el('span','',file.name));const remove=el('button','','×');remove.type='button';remove.setAttribute('aria-label','Убрать '+file.name);remove.addEventListener('click',()=>{files.splice(index,1);if(target==='attachment-preview')resetMessageOperation();else resetTaskOperation();renderFiles(files,target);});pill.append(remove);return pill;}));}
  function addFiles(input,collection,target){const allowed=/\.(pdf|docx?|xlsx?|pptx?)$/i;for(const file of input.files){if(!(file.type.startsWith('image/')||file.type.startsWith('video/')||allowed.test(file.name))){notify('Этот тип файла не поддерживается',true);continue;}if(file.size>20_000_000){notify('Файл больше 20 МБ',true);continue;}collection.push(file);}input.value='';if(target==='attachment-preview')resetMessageOperation();else resetTaskOperation();renderFiles(collection,target);}
  async function uploadFiles(files){const ids=[];for(const file of files){const form=new FormData();form.set('file',file,file.name);const data=await request('/uploads',{method:'POST',body:form,timeoutMs:90000});if(!data.id)throw new Error('Сервер не подтвердил загрузку '+file.name);ids.push(data.id);}return ids;}
  async function importFile(event){event.preventDefault();const path=$('file-path').value.trim();const threadId=state.selectedThread?.id;if(!path||!threadId){notify('Выберите чат и укажите путь к файлу',true);return;}const button=$('file-import-form').querySelector('[type="submit"]');button.disabled=true;try{const file=await request('/files/import',{method:'POST',body:{path,threadId},timeoutMs:30000});if(!file.id)throw new Error('Сервер не подтвердил доступ к файлу');notify('Файл найден. Скачиваем…');await downloadAttachment(file);$('file-path').value='';$('file-import-form').hidden=true;$('file-import-toggle').setAttribute('aria-expanded','false');notify('Файл получен');}catch(error){notify(error.message,true);}finally{button.disabled=false;}}
  async function sendMessage(event){event.preventDefault();if(state.busy||!state.selectedThread)return;const text=$('message-text').value.trim();if(!text&&!state.messageFiles.length)return;const thread=state.selectedThread;const isChatGPT=kind(thread)==='ChatGPT';
    state.busy=true;const submit=$('message-form').querySelector('[type="submit"]');submit.disabled=true;
    try{state.messageOperationId ||= newOperationId();const attachmentIds=state.messageAttachmentIds || await uploadFiles(state.messageFiles);state.messageAttachmentIds=attachmentIds;const body={text,attachmentIds,operationId:state.messageOperationId};if(!isChatGPT){if(state.model)body.model=state.model;if(state.thinking)body.thinking=state.thinking;}
      const result=await request('/threads/'+encodeURIComponent(thread.id)+'/messages',{method:'POST',body});if(result.accepted!==true||!result.operationId)throw new Error(result.error||'Сервер не подтвердил приём задания');trackOperation(result.operationId,'followup',thread.id);
      if(state.selectedThread?.id===thread.id){$('message-text').value='';state.messageFiles=[];resetMessageOperation();renderFiles(state.messageFiles,'attachment-preview');state.threadMessages.push({id:'local-'+result.operationId,operationId:String(result.operationId),role:'user',text,createdAt:new Date().toISOString(),queued:true});renderTimeline(true);}notify('Принято в очередь. Статус виден под сообщением и на главной.');refresh(true);
    }catch(error){notify(error.message,true);}finally{state.busy=false;submit.disabled=false;}
  }
  async function createTask(event){event.preventDefault();if(state.busy)return;const projectId=$('project-select').value,text=$('new-text').value.trim();if(!projectId||!text){notify('Выберите проект и опишите задачу',true);return;}const project=state.projects.find(p=>(p.projectId||p.id)===projectId);if(!project||projectSurface(project)!==state.surface){notify('Выберите проект в текущем разделе',true);return;}const isChatGPT=projectSurface(project)==='chatgpt';
    state.busy=true;const submit=$('new-form').querySelector('[type="submit"]');submit.disabled=true;
    try{state.taskOperationId ||= newOperationId();const attachmentIds=state.taskAttachmentIds || await uploadFiles(state.newFiles);state.taskAttachmentIds=attachmentIds;const body={projectId,text,attachmentIds,operationId:state.taskOperationId};if(!isChatGPT){if(state.model)body.model=state.model;if(state.thinking)body.thinking=state.thinking;}const result=await request('/tasks',{method:'POST',body});if(result.accepted!==true||!result.operationId)throw new Error('Сервер не подтвердил создание задачи');trackOperation(result.operationId,'new_task',projectId);
      $('new-text').value='';state.newFiles=[];resetTaskOperation();renderFiles(state.newFiles,'new-attachment-preview');show('home');notify('Задача принята. Для проверки результата нажмите «Обновить».');refresh(true);
    }catch(error){notify(error.message,true);}finally{state.busy=false;submit.disabled=false;}
  }
  async function threadAction(action,value='',thread=state.selectedThread){
    if(!thread)return;
    const key=[thread.id,action,value].join(':');
    state.actionKeys[key] ||= newOperationId();
    $('thread-actions').hidden=true;$('thread-actions-toggle').setAttribute('aria-expanded','false');$('thread-rename-form').hidden=true;
    try{
      const result=await request('/threads/'+encodeURIComponent(thread.id)+'/actions',{method:'POST',body:{action,value,operationId:state.actionKeys[key]}});
      if(result.accepted!==true||!result.operationId)throw new Error('Сервер не подтвердил действие');
      trackOperation(result.operationId,'thread_'+action,thread.id);delete state.actionKeys[key];
      notify('Действие принято. Для проверки результата нажмите «Обновить».');await refresh(true);
    }catch(error){notify(error.message,true);}
  }
  async function loadOlderHistory(){
    if(!state.selectedThread||!state.historyCursor||state.historyPending)return;
    const threadId=state.selectedThread.id,cursor=state.historyCursor;
    state.historyPending=true;renderHistoryButton();
    try{
      if(cursor.startsWith('local:v2:')){
        const data=await request('/threads/'+encodeURIComponent(threadId)+'?before='+encodeURIComponent(cursor),{etag:false,timeoutMs:25000});
        if(state.selectedThread?.id!==threadId)return;
        const existing=new Set(state.threadMessages.map(row=>String(row.id)));
        state.threadMessages=[...(data.messages||[]).filter(row=>!existing.has(String(row.id))),...state.threadMessages];
        state.historyCursor=data.nextCursor||null;state.historyExpanded=true;state.historyPending=false;renderTimeline();renderHistoryButton();return;
      }
      state.historyOperationId ||= newOperationId();const result=await request('/threads/'+encodeURIComponent(threadId)+'/history',{method:'POST',body:{cursor,operationId:state.historyOperationId}});if(result.accepted!==true||!result.operationId)throw new Error('Сервер не подтвердил запрос истории');trackOperation(result.operationId,'view_more',threadId);await refresh(true);
    }
    catch(error){state.historyPending=false;renderHistoryButton();notify(error.message,true);}
  }
  function renderSettings(){
    for(const id of ['model-select','new-model-select','thread-model-select']){const model=$(id);model.replaceChildren(new Option('По умолчанию',''));Object.keys(MODEL_EFFORTS).forEach(m=>model.add(new Option(m,m)));model.value=state.model;}
    for(const id of ['effort-select','new-effort-select','thread-effort-select']){const effort=$(id);effort.replaceChildren(new Option('По умолчанию',''));(MODEL_EFFORTS[state.model]||[]).forEach(e=>effort.add(new Option(EFFORT_LABELS[e],e)));effort.disabled=!state.model;effort.value=(MODEL_EFFORTS[state.model]||[]).includes(state.thinking)?state.thinking:'';}
  }
  function persistSettings(){sessionStorage.setItem('miniapp-model',state.model);sessionStorage.setItem('miniapp-thinking',state.thinking);renderModelNote();}
  async function toggleVoice(){if(state.recorder){state.recorder.stop();return;}if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){notify('Запись голоса недоступна на этом устройстве',true);return;}try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});const recorder=new MediaRecorder(stream);state.recordedChunks=[];recorder.ondataavailable=e=>{if(e.data.size)state.recordedChunks.push(e.data);};recorder.onstop=async()=>{stream.getTracks().forEach(track=>track.stop());for(const button of [$('voice-button'),$('new-voice-button')])button.classList.remove('recording');state.recorder=null;const blob=new Blob(state.recordedChunks,{type:recorder.mimeType||'audio/webm'});if(blob.size<500)return;if(blob.size>20_000_000){notify('Запись больше 20 МБ',true);return;}const form=new FormData();form.set('file',blob,'voice.webm');try{notify('Распознаём голос…');const result=await request('/voice/transcribe',{method:'POST',body:form,timeoutMs:180000});const transcript=result.transcript||result.text;if(!transcript)throw new Error('Речь не распознана');state.voiceText=String(transcript).slice(0,4000);$('voice-transcript').textContent=state.voiceText;$('confirm-dialog').hidden=false;}catch(error){notify(error.message,true);}};recorder.start();state.recorder=recorder;for(const button of [$('voice-button'),$('new-voice-button')])button.classList.add('recording');notify('Идёт запись. Нажмите ещё раз, чтобы завершить.');setTimeout(()=>{if(state.recorder===recorder)recorder.stop();},180000);}catch(error){notify('Не удалось получить доступ к микрофону',true);}}
  function openDrawer(){document.body.classList.add('drawer-open');$('mobile-drawer').removeAttribute('inert');$('mobile-drawer').setAttribute('aria-hidden','false');$('drawer-open').setAttribute('aria-expanded','true');$('drawer-backdrop').hidden=false;}
  function closeDrawer(){document.body.classList.remove('drawer-open');$('mobile-drawer').setAttribute('inert','');$('mobile-drawer').setAttribute('aria-hidden','true');$('drawer-open').setAttribute('aria-expanded','false');$('drawer-backdrop').hidden=true;}
  function installEvents(){document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{if(button.dataset.filter){state.filter=button.dataset.filter;}show(button.dataset.view);if(button.classList.contains('drawer-search'))$('chat-search').focus();}));$('thread-back').addEventListener('click',()=>show('chats'));$('new-back').addEventListener('click',()=>show('projects'));
    $('thread-artifacts-toggle').addEventListener('click',openArtifacts);$('artifact-close').addEventListener('click',closeArtifacts);$('artifact-refresh').addEventListener('click',loadArtifacts);$('artifacts-drawer').addEventListener('click',event=>{if(event.target===$('artifacts-drawer'))closeArtifacts();});
    $('drawer-open').addEventListener('click',openDrawer);$('drawer-close').addEventListener('click',closeDrawer);$('drawer-backdrop').addEventListener('click',closeDrawer);$('mobile-new-task').addEventListener('click',()=>show('new'));document.addEventListener('keydown',event=>{if(event.key==='Escape'){if(!$('artifacts-drawer').hidden)closeArtifacts();else closeDrawer();}});
    document.querySelectorAll('[data-surface]').forEach(button=>button.addEventListener('click',()=>switchSurface(button.dataset.surface)));
    $('mode-chats').addEventListener('click',()=>{state.filter='all';document.querySelectorAll('[data-chat-filter]').forEach(b=>b.classList.toggle('selected',b.dataset.chatFilter==='all'));show('chats');renderThreads();});
    $('mode-work').addEventListener('click',()=>{state.filter='active';document.querySelectorAll('[data-chat-filter]').forEach(b=>b.classList.toggle('selected',b.dataset.chatFilter==='active'));show('chats');renderThreads();});
    $('composer-settings-toggle').addEventListener('click',()=>{const button=$('composer-settings-toggle');const open=button.getAttribute('aria-expanded')!=='true';button.setAttribute('aria-expanded',String(open));$('thread-model-controls').hidden=!open;});
    $('thread-actions-toggle').addEventListener('click',()=>{dismissChatKeyboard();const panel=$('thread-actions');panel.hidden=!panel.hidden;$('thread-actions-toggle').setAttribute('aria-expanded',String(!panel.hidden));if(panel.hidden){$('thread-archive').dataset.confirm='';$('thread-archive').textContent='Архивировать чат';$('thread-rename-form').hidden=true;}});
    $('thread-load-older').addEventListener('click',loadOlderHistory);
    $('thread-rename').addEventListener('click',()=>{$('thread-rename-form').hidden=false;$('thread-rename-input').value=plainTitle(state.selectedThread||{},'');$('thread-rename-input').focus();});
    $('thread-rename-save').addEventListener('click',()=>{const value=$('thread-rename-input').value.trim();if(value)threadAction('rename',value);else notify('Введите название чата',true);});
    $('thread-rename-input').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();$('thread-rename-save').click();}});
    $('thread-pin').addEventListener('click',()=>threadAction(state.selectedThread?.pinned?'unpin':'pin'));
    $('thread-archive').addEventListener('click',()=>{const button=$('thread-archive');if(button.dataset.confirm==='yes'){button.dataset.confirm='';button.textContent='Архивировать чат';threadAction('archive');}else{button.dataset.confirm='yes';button.textContent='Подтвердить архивацию';}});
    $('new-task-home').addEventListener('click',()=>show('new'));$('usage-strip').addEventListener('click',()=>{show('usage');refreshUsage();});$('usage-refresh').addEventListener('click',()=>refreshUsage(true));$('refresh').addEventListener('click',()=>refresh());$('settings-refresh').addEventListener('click',()=>refresh());$('load-more').addEventListener('click',()=>loadThreads(true));
    $('chat-search').addEventListener('input',e=>{state.search=e.target.value.toLocaleLowerCase('ru-RU').trim();renderThreads();});document.querySelectorAll('[data-chat-filter]').forEach(button=>button.addEventListener('click',()=>{state.filter=button.dataset.chatFilter;document.querySelectorAll('[data-chat-filter]').forEach(b=>b.classList.toggle('selected',b===button));renderThreads();}));
    $('message-files').addEventListener('change',e=>addFiles(e.target,state.messageFiles,'attachment-preview'));$('new-files').addEventListener('change',e=>addFiles(e.target,state.newFiles,'new-attachment-preview'));$('project-select').addEventListener('change',()=>{resetTaskOperation();renderModelNote();});$('message-text').addEventListener('input',resetMessageOperation);$('new-text').addEventListener('input',resetTaskOperation);
    $('message-form').addEventListener('submit',sendMessage);$('new-form').addEventListener('submit',createTask);$('file-import-form').addEventListener('submit',importFile);$('file-import-toggle').addEventListener('click',()=>{const form=$('file-import-form');form.hidden=!form.hidden;$('file-import-toggle').setAttribute('aria-expanded',String(!form.hidden));if(!form.hidden)$('file-path').focus();});
    for(const id of ['model-select','new-model-select','thread-model-select'])$(id).addEventListener('change',e=>{state.model=e.target.value;if(!(MODEL_EFFORTS[state.model]||[]).includes(state.thinking))state.thinking='';resetMessageOperation();resetTaskOperation();renderSettings();persistSettings();});
    for(const id of ['effort-select','new-effort-select','thread-effort-select'])$(id).addEventListener('change',e=>{state.thinking=e.target.value;resetMessageOperation();resetTaskOperation();renderSettings();persistSettings();});
    $('voice-button').addEventListener('click',toggleVoice);$('new-voice-button').addEventListener('click',toggleVoice);$('voice-cancel').addEventListener('click',()=>{$('confirm-dialog').hidden=true;state.voiceText='';});$('voice-confirm').addEventListener('click',()=>{const input=state.view==='new'?'new-text':'message-text';$(input).value=[$(input).value.trim(),state.voiceText].filter(Boolean).join('\n');if(input==='new-text')resetTaskOperation();else resetMessageOperation();$('confirm-dialog').hidden=true;state.voiceText='';$(input).focus();});
    $('thread-decisions-open').addEventListener('click',()=>openDecisions());$('decision-close').addEventListener('click',closeDecisions);
    $('decision-submit').addEventListener('click',submitDecision);$('decision-refresh').addEventListener('click',()=>void loadDecisions());
    $('decision-backdrop').addEventListener('click',event=>{if(event.target===$('decision-backdrop'))closeDecisions();});
    document.addEventListener('keydown',event=>{const dialog=$('decision-backdrop');if(dialog.hidden)return;if(event.key==='Escape'){event.preventDefault();closeDecisions();return;}if(event.key!=='Tab')return;const focusable=[...dialog.querySelectorAll('button:not([hidden]):not(:disabled),textarea:not([hidden]):not(:disabled)')].filter(node=>node.getClientRects().length);if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}});
    tg?.BackButton?.onClick?.(()=>{if(state.view==='kanban'&&kanban?.closeDetail())return;if(!$('decision-backdrop').hidden)closeDecisions();else if(document.body.classList.contains('drawer-open'))closeDrawer();else show('chats');});document.addEventListener('visibilitychange',()=>{connectionVersion++;if(document.hidden)setConnection('unknown','Панель была скрыта. При возвращении проверим доступность компьютера.');else refresh(true,true);scheduleDeliveryWatch();scheduleDecisionWatch();});
    window.addEventListener('offline',()=>{connectionVersion++;setConnection(false,'На устройстве нет сети. После восстановления проверим соединение.');});
    window.addEventListener('online',()=>refresh(true,true));
  }
  async function refreshThread(){
    const id=state.selectedThread?.id;if(!id)return false;
    try{const data=await request('/threads/'+encodeURIComponent(id));lastFetch.detail=Date.now();if(data.notModified||state.selectedThread?.id!==id)return true;
      if(data.thread?.id===id){state.selectedThread=data.thread;renderThreadPlan(data.thread);$('thread-status').textContent=status(data.thread);$('thread-status').classList.toggle('idle',!active(data.thread));$('thread-title').textContent=plainTitle(data.thread,'Чат');}
      const received=data.messages||data.turns||[];
      const queued=pendingMessagesNotIn(received);
      const receivedIds=new Set(received.map(row=>String(row.id)));
      if(data.localHistory&&state.historyExpanded&&!state.threadMessages.some(row=>!row.queued&&receivedIds.has(String(row.id))))state.historyExpanded=false;
      const retained=data.localHistory&&state.historyExpanded?state.threadMessages.filter(row=>!row.queued&&!receivedIds.has(String(row.id))):[];
      const next=[...retained,...received,...queued];
      const fingerprint=rows=>JSON.stringify(rows.map(row=>[row.id,row.role,row.text,row.createdAt,row.queued,row.delivery,row.operationId,(row.attachments||[]).length]));
      if(fingerprint(next)!==fingerprint(state.threadMessages)){state.threadMessages=next;renderTimeline();}
      if(!(data.localHistory&&state.historyExpanded)&&state.historyCursor!==data.nextCursor){state.historyCursor=data.nextCursor||null;state.historyPending=false;state.historyOperationId='';renderHistoryButton();}
      return true;
    }catch{return false;}
  }
  async function refresh(quiet=false,recheck=false){
    if(state.refreshing){if(recheck)state.recheckRequested=true;return;}state.refreshing=true;
    const version=connectionVersion,responses=connectionResponses,previousPhase=$('connection').dataset.state||'unknown',previousDescription=$('connection-detail').textContent;
    if(!document.hidden)setConnection('checking');
    try{const now=Date.now(),force=!quiet,jobs=[pollOperations()];
      if(['chats','home'].includes(state.view)&&(force||recheck||!state.threads.length||now-lastFetch.threads>=20000)){lastFetch.threads=now;jobs.push(loadThreads());}
      if(['projects','new','home'].includes(state.view)&&(force||recheck||now-lastFetch.projects>=60000)){lastFetch.projects=now;jobs.push(loadProjects());}
      // Account limits are independent of chat delivery and must not lock its refresh.
      refreshUsage(force);
      if(state.view==='archived'&&(force||now-lastFetch.archived>=20000)){lastFetch.archived=now;jobs.push(loadArchived());}
      if(state.view==='kanban'&&kanban)jobs.push(kanban.refresh());
      if(state.view==='thread'&&state.selectedThread&&(force||recheck||now-lastFetch.detail>=4000)){lastFetch.detail=now;jobs.push(refreshThread());jobs.push(loadDecisions(state.selectedThread.id));}
      const results=await Promise.allSettled(jobs.map(job=>Promise.resolve(job).then(value=>{
        // A slow independent data source must not leave the whole panel looking frozen.
        if(version===connectionVersion&&!document.hidden&&connectionResponses>responses)
          setConnection(true,'Компьютер ответил '+usageTimestamp(Date.now()/1000)+'. Остальные данные раздела обновляются.');
        return value;
      }))),success=results.every(result=>result.status==='fulfilled'&&result.value!==false);if(success)state.lastConnectionError='';
      if(version===connectionVersion&&!document.hidden){if(connectionResponses>responses)setConnection(true,success?undefined:'Ответ компьютера получен '+usageTimestamp(Date.now()/1000)+'. Часть данных не обновлена; проверьте лимиты. Это последняя проверка, постоянного соединения нет.');else setConnection(previousPhase,previousDescription);}
      if(force)notify(state.view==='usage'?'Обновление лимитов запрошено. Результат показан в разделе лимитов.':success?'Данные текущего раздела обновлены':'Часть данных не обновлена. Проверьте соединение.',!success);
    }finally{state.refreshing=false;if(state.recheckRequested&&!document.hidden){state.recheckRequested=false;refresh(true,true);}else scheduleDeliveryWatch();scheduleDecisionWatch();}
  }
  function boot(){
    if(window.CtdKanban)kanban=window.CtdKanban.mount({host:$('kanban-host'),request:(path,options={})=>request(path,{...options,etag:false,timeoutMs:15000}),notify});
    const filesButton=el('button','thread-artifacts-toggle','Файлы');filesButton.id='thread-artifacts-toggle';filesButton.type='button';filesButton.setAttribute('aria-haspopup','dialog');$('thread-actions-toggle').before(filesButton);
    const progressButton=el('button','thread-progress-toggle','Прогресс');progressButton.id='thread-progress-toggle';progressButton.type='button';progressButton.setAttribute('aria-controls','thread-progress');progressButton.setAttribute('aria-expanded','false');progressButton.setAttribute('aria-label','Показать прогресс этой задачи');progressButton.addEventListener('click',()=>{if(progressButton.getAttribute('aria-expanded')==='true')setProgressExpanded(false);else openCurrentProgress(state.selectedThread?.id);});filesButton.after(progressButton);
    const artifactsBackdrop=el('div','artifact-backdrop');artifactsBackdrop.id='artifacts-drawer';artifactsBackdrop.hidden=true;artifactsBackdrop.setAttribute('inert','');artifactsBackdrop.setAttribute('role','dialog');artifactsBackdrop.setAttribute('aria-modal','true');artifactsBackdrop.setAttribute('aria-labelledby','artifact-title');
    const artifactsPanel=el('section','artifact-drawer');const artifactsHead=el('header','artifact-head');const artifactsHeading=el('div','');artifactsHeading.append(el('small','overline','РЕЗУЛЬТАТЫ ЗАДАЧИ'));const artifactTitle=el('h2','','Файлы и артефакты');artifactTitle.id='artifact-title';artifactsHeading.append(artifactTitle);const artifactClose=el('button','artifact-close','×');artifactClose.id='artifact-close';artifactClose.type='button';artifactClose.setAttribute('aria-label','Закрыть список файлов');artifactsHead.append(artifactsHeading,artifactClose);
    const artifactToolbar=el('div','artifact-toolbar');const artifactStatus=el('p','artifact-status','');artifactStatus.id='artifact-status';const artifactRefresh=el('button','artifact-refresh','Обновить');artifactRefresh.id='artifact-refresh';artifactRefresh.type='button';artifactToolbar.append(artifactStatus,artifactRefresh);const artifactList=el('div','artifact-list');artifactList.id='artifact-list';artifactsPanel.append(artifactsHead,artifactToolbar,artifactList);artifactsBackdrop.append(artifactsPanel);document.body.append(artifactsBackdrop);renderArtifacts();
    const planBanner=el('div','thread-plan');planBanner.id='thread-plan';planBanner.hidden=true;$('thread-view').insertBefore(planBanner,$('thread-actions'));
    const progressHost=el('div','task-progress-host');progressHost.id='thread-progress';progressHost.tabIndex=-1;$('thread-view').insertBefore(progressHost,planBanner);
    if(window.CtdTaskProgress)taskProgress=window.CtdTaskProgress.mount({host:progressHost,request:(path,options={})=>request(path,{...options,etag:false,timeoutMs:6000}),onOpen:()=>{if(progressHost.classList.contains('task-progress-expanded'))setProgressExpanded(false);else openCurrentProgress(state.selectedThread?.id);}});
    progressHost.append(planBanner);
    const decisionBar=el('div','thread-decisions-bar');decisionBar.id='thread-decisions-bar';decisionBar.hidden=true;decisionBar.append(el('span','thread-decisions-icon','!'));const decisionCount=el('span','thread-decisions-count');decisionCount.id='thread-decisions-count';const decisionOpen=el('button','thread-decisions-open','Открыть');decisionOpen.id='thread-decisions-open';decisionOpen.type='button';decisionBar.append(decisionCount,decisionOpen);$('thread-view').insertBefore(decisionBar,$('thread-load-older'));
    const menuActions=[['decisions','Решения',()=>openDecisions()],['files','Файлы и артефакты',()=>openArtifacts()],['progress','Прогресс задачи',()=>openCurrentProgress(state.selectedThread?.id)],['model','Модель и интенсивность',()=>{if(kind(state.selectedThread)==='ChatGPT'){show('settings');return;}$('composer-settings-toggle').setAttribute('aria-expanded','true');$('thread-model-controls').hidden=false;}],['usage','Лимиты Codex',()=>{show('usage');refreshUsage();}],['refresh','Обновить чат',()=>refresh(false,true)]];
    for(const [id,label,action] of menuActions){const button=el('button','thread-mobile-action',label);button.id='thread-menu-'+id;button.type='button';button.addEventListener('click',()=>{closeThreadMenu();action();});$('thread-actions').prepend(button);}
    $('thread-actions').append(document.querySelector('#thread-view .transfer'));
    $('voice-button').textContent='🎙';installChatViewport();
    installEvents();try{state.surface='codex';}catch{state.surface='codex';}state.model=sessionStorage.getItem('miniapp-model')||'';state.thinking=sessionStorage.getItem('miniapp-thinking')||'';if(!MODEL_EFFORTS[state.model]){state.model='';state.thinking='';}try{state.operations=(JSON.parse(sessionStorage.getItem('miniapp-operations')||'[]')||[]).filter(op=>/^\d{19}$/.test(String(op.id))).slice(0,8).map(op=>({...op,id:String(op.id),ok:typeof op.ok==='boolean'?op.ok:null,deliveryStage:DELIVERY_STAGES.has(op.deliveryStage)?op.deliveryStage:'',historyReceived:op.historyReceived===true,text:''}));}catch{state.operations=[];}renderSurface();renderOperations();renderSettings();renderModelNote();renderUsage();if(tg){tg.ready();tg.expand();tg.setHeaderColor?.('#080808');tg.setBackgroundColor?.('#080808');}if(!tg?.initData){setConnection(false,'Откройте Mini App через бота в Telegram');$('recent-list').replaceChildren(el('div','empty-mini','Откройте панель через Telegram'));$('thread-list').replaceChildren(el('div','empty-state','Откройте панель через Telegram'));$('project-list').replaceChildren(el('div','empty-state','Откройте панель через Telegram'));return;}refresh(true);}
  boot();
})();
