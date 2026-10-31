
(function(){
  var each=function(sel,fn){[].forEach.call(document.querySelectorAll(sel),fn)};
  // a video or image that fails to load hides its box instead of showing a broken one
  each('figure.vid video',function(v){v.addEventListener('error',function(){v.closest('figure').classList.add('hidden')})});
  each('img',function(i){
    function bad(){var d=document.createElement('div');d.className='noimg';i.replaceWith(d)}
    i.addEventListener('error',bad); if(i.complete&&i.getAttribute('src')&&i.naturalWidth===0&&i.loading!=='lazy')bad();
  });

  // home page: sport filter + timeline grouped by day / week / month, with a period picker
  var state={sport:'all',group:'day',period:'all'};
  var sel=document.getElementById('period');
  function fillPeriods(){
    if(!sel) return;
    sel.innerHTML='<option value="all">All '+(state.group==='day'?'days':state.group+'s')+'</option>';
    each('.timeline[data-group="'+state.group+'"] > .day',function(li){
      var o=document.createElement('option'); o.value=li.dataset.key; o.textContent=li.dataset.label; sel.appendChild(o);
    });
    sel.value='all'; state.period='all';
  }
  function apply(){
    each('.f',function(b){b.classList.toggle('on',b.dataset.f===state.sport)});
    each('.g',function(b){b.classList.toggle('on',b.dataset.g===state.group)});
    each('[data-sport]',function(el){el.classList.toggle('hidden',state.sport!=='all'&&el.dataset.sport!==state.sport)});
    each('.timeline',function(ol){ol.classList.toggle('hidden',ol.dataset.group!==state.group)});
    each('.timeline > .day',function(d){
      var out=state.period!=='all'&&d.dataset.key!==state.period;
      d.classList.toggle('empty',out||!d.querySelector('.entry:not(.hidden)'));
    });
    var h=[]; if(state.sport!=='all')h.push('sport='+encodeURIComponent(state.sport)); if(state.group!=='day')h.push('by='+state.group);
    try{history.replaceState(null,'',h.length?'#'+h.join('&'):location.pathname)}catch(e){}
  }
  each('.f',function(b){b.addEventListener('click',function(){state.sport=b.dataset.f;apply()})});
  each('.g',function(b){b.addEventListener('click',function(){state.group=b.dataset.g;fillPeriods();apply()})});
  if(sel) sel.addEventListener('change',function(){state.period=sel.value;apply()});
  if(document.querySelector('.f')){
    location.hash.slice(1).split('&').forEach(function(kv){
      var p=kv.split('='),v=decodeURIComponent(p[1]||p[0]||'');
      if(p[0]==='by'&&/^(day|week|month)$/.test(v))state.group=v;
      else if(v&&document.querySelector('.f[data-f="'+v+'"]'))state.sport=v;
    });
    fillPeriods(); apply();
  }

  // clip page: interactive viewer, loaded on click; tabs switch between Realism and classic 3D
  each('.viewer',function(v){
    var b=v.querySelector('.load'); if(!b) return;
    b.addEventListener('click',function(){
      var f=document.createElement('iframe'); f.src=v.dataset.src; f.allow='fullscreen'; f.setAttribute('allowfullscreen','');
      f.title='Interactive 3D viewer'; v.innerHTML=''; v.appendChild(f);
    });
  });
  each('.vt',function(t){t.addEventListener('click',function(){
    var v=document.querySelector('.viewer'), f=v.querySelector('iframe'), a=document.querySelector('.vopen'), s=v.querySelector('.load small');
    each('.vt',function(x){x.classList.toggle('on',x===t)});
    v.dataset.src=t.dataset.src; if(a)a.href=t.dataset.src; if(s)s.textContent=t.dataset.size+' · three.js'; if(f)f.src=t.dataset.src;
  })});
})();
