(() => {
  'use strict';
  const request = async (path, options={}) => {
    const abort=new AbortController(),cancel=()=>abort.abort();
    if(options.signal?.aborted)cancel();else options.signal?.addEventListener('abort',cancel,{once:true});
    const timer=setTimeout(cancel,options.timeoutMs||15000);
    try{
      const response = await fetch('/api' + path, {signal:abort.signal, cache:'no-store', credentials:'same-origin'});
      if(!response.ok)throw new Error('Источник временно недоступен');
      return await response.json();
    }finally{clearTimeout(timer);options.signal?.removeEventListener('abort',cancel);}
  };
  const selected = window.CtdTaskProgress.mount({host:document.getElementById('selected-progress'),request});
  const open = id => {
    if(!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id))return;
    document.getElementById('selection-note').hidden=true;
    selected.select(id);selected.start();
    const url=new URL(location.href);url.searchParams.set('thread',id);history.replaceState(null,'',url);
  };
  const id=new URL(location.href).searchParams.get('thread');if(id)open(id);
  addEventListener('pagehide',()=>{selected.destroy();},{once:true});
})();
