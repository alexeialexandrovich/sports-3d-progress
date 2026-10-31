
(function(){
  var btns=document.querySelectorAll('.f');
  function apply(f){
    btns.forEach(function(b){b.classList.toggle('on',b.dataset.f===f)});
    document.querySelectorAll('[data-sport]').forEach(function(el){el.classList.toggle('hidden',f!=='all'&&el.dataset.sport!==f)});
    document.querySelectorAll('.day').forEach(function(d){d.classList.toggle('empty',!d.querySelector('.entry:not(.hidden)'))});
    try{ if(f==='all') history.replaceState(null,'',location.pathname); else history.replaceState(null,'','#'+encodeURIComponent(f)); }catch(e){}
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.dataset.f)})});
  if(btns.length){var h=decodeURIComponent(location.hash.slice(1));apply([].some.call(btns,function(b){return b.dataset.f===h})?h:'all');}
  document.querySelectorAll('.viewer').forEach(function(v){
    var b=v.querySelector('.load'); if(!b) return;
    b.addEventListener('click',function(){
      var f=document.createElement('iframe'); f.src=v.dataset.src; f.allow='fullscreen'; f.setAttribute('allowfullscreen','');
      f.title='Interactive 3D viewer'; v.innerHTML=''; v.appendChild(f);
    });
  });
  // play muted renders when scrolled into view? keep it light: only on hover/tap via controls.
})();
