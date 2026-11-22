(function(){
var C=window.OOZ||{}, Q=location.search.indexOf('preview')>=0;
function $(s){return Array.prototype.slice.call(document.querySelectorAll(s));}
$('[data-ooz]').forEach(function(e){var v=C[e.getAttribute('data-ooz')]; if(v!=null) e.textContent=v;});
$('.socials').forEach(function(e){(C.socials||[]).forEach(function(s){
  var b=document.createElement('b'); b.textContent=s[0]; var t=document.createElement('span'); t.textContent=s[1];
  e.appendChild(b); e.appendChild(t);});});
$('.schedule').forEach(function(e){(C.schedule||[]).forEach(function(s){var d=document.createElement('div');
  d.textContent=s; e.appendChild(d);});});
// rotating line: socials, then the schedule, then the commission link
$('.rot').forEach(function(e){
  var items=(C.socials||[]).map(function(s){return [s[0],s[1]];});
  (C.schedule||[]).forEach(function(s){items.push(['Schedule',s]);});
  if(C.commissionLink) items.push(['Commissions',C.commissionLink]);
  var spans=items.map(function(it){var s=document.createElement('span'); var b=document.createElement('b');
    b.textContent=it[0]; s.appendChild(b); s.appendChild(document.createTextNode(it[1])); e.appendChild(s); return s;});
  var i=0; if(spans.length){spans[0].className='on';
    setInterval(function(){spans[i].className=''; i=(i+1)%spans.length; spans[i].className='on';},6000);}
});
// countdown (Starting Soon): restarts each time the scene is shown
$('[data-countdown]').forEach(function(e){
  var end=Date.now()+(Number(C.countdownMinutes)||5)*60000;
  function tick(){var s=Math.max(0,Math.round((end-Date.now())/1000));
    e.textContent=s>0?(Math.floor(s/60)+':'+('0'+s%60).slice(-2)):'Starting now';}
  tick(); setInterval(tick,1000);
});
// slideshow (Be Right Back)
if(!Q) $('[data-preview-only]').forEach(function(e){e.remove();});   // in OBS the video plays here
$('.slides').forEach(function(e){var imgs=e.querySelectorAll('img'), i=0; if(!imgs.length) return;
  imgs[0].className='on'; setInterval(function(){imgs[i].className=''; i=(i+1)%imgs.length; imgs[i].className='on';},7000);});
// no face camera: the studio mark fills its window
if(C.faceCam===false){ $('.win[data-sample="face"]').forEach(function(w){w.removeAttribute('data-sample');
  w.innerHTML='<div style="text-align:center"><img src="assets/mark.svg" style="width:110px;height:110px">'+
    '<div class="kicker" style="margin-top:16px">Inking live</div></div>';}); }
// preview mode (only for the preview pictures): sample art in the camera windows
if(Q && document.body.classList.contains('clear')){var bgi=document.createElement('img'); bgi.className='sample';
  bgi.src='assets/art/01-samurai.jpg'; bgi.style.cssText='position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:grayscale(1);z-index:-1';
  document.body.insertBefore(bgi, document.body.firstChild);}
if(Q){ $('.win[data-sample]').forEach(function(w){var k=w.getAttribute('data-sample'); w.classList.add('sample');
    if(k==='chat'){w.innerHTML='<div class="chat-sample"><p><b>inkfan_22</b>the hatching on the hair is crazy</p>'+
      '<p><b>mangamike</b>what pen is that?</p><p><b>sketchdaily</b>first time catching this live \ud83d\udd25</p>'+
      '<p><b>oozstudioink1</b>Sakura nib, 0.3 \u270d\ud83c\udffe</p><p><b>lena.draws</b>do Gojo next!</p></div>';}
    else {var i=document.createElement('img'); i.className='sample';
      i.src=k==='face'?'assets/art/04-fighter-and-beast.jpg':'assets/art/01-samurai.jpg'; w.appendChild(i);} }); }
})();
