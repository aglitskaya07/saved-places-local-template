'use strict';
(() => {
 const cards=[...document.querySelectorAll('.card')], city=document.querySelector('#city'), mood=document.querySelector('#mood'), query=document.querySelector('#query');
 let page=0;
 function render(){
  const q=query.value.trim().toLocaleLowerCase();
  const matches=cards.filter(c=>(!city.value||c.dataset.city===city.value)&&(!mood.value||c.dataset.moods.split(' ').includes(mood.value))&&(!q||c.dataset.search.toLocaleLowerCase().includes(q)));
  const pages=Math.max(1,Math.ceil(matches.length/8));page=Math.max(0,Math.min(page,pages-1));
  cards.forEach(c=>c.hidden=true);matches.slice(page*8,page*8+8).forEach(c=>c.hidden=false);
  document.querySelector('#count').textContent='Мест: '+matches.length;
  document.querySelector('#empty').hidden=matches.length>0;
  document.querySelector('#pagination').hidden=pages<=1;
  document.querySelector('#page').textContent=(page+1)+' / '+pages;
  document.querySelector('#previous').disabled=page===0;document.querySelector('#next').disabled=page===pages-1;
 }
 document.querySelector('#filters').addEventListener('submit',e=>e.preventDefault());
 [city,mood,query].forEach(el=>el.addEventListener('input',()=>{page=0;render();}));
 document.querySelector('#previous').addEventListener('click',()=>{page--;render();document.querySelector('#count').scrollIntoView();});
 document.querySelector('#next').addEventListener('click',()=>{page++;render();document.querySelector('#count').scrollIntoView();});
 render();
})();
