(()=>{
'use strict';
const $=selector=>document.querySelector(selector),form=$('#admin-key-form'),list=$('#review-list'),message=$('#admin-message');
let key='';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function showMessage(text,error=false){message.textContent=text;message.className='message'+(error?' error':'');message.hidden=false}
async function api(url,options={}){
 const response=await fetch(url,{...options,headers:{'X-Admin-Key':key,...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
 const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data;
}
async function load(){
 try{
  const data=await api('/api/admin/reviews');
  list.innerHTML=data.reviews.length?data.reviews.map(user=>'<article class="review-item"><h3>'+esc(user.name)+' · '+esc(user.role)+'</h3><p>Mobile: '+esc(user.phone)+'</p><p>Credential: '+esc(user.credential_type)+' · '+esc(user.credential_id)+'</p><div class="review-actions"><button class="button primary" data-decision="approved" data-user="'+esc(user.id)+'">Approve after external check</button><button class="button" data-decision="rejected" data-user="'+esc(user.id)+'">Reject</button></div></article>').join(''):'<div class="empty-state">No professional accounts are awaiting review.</div>';
 }catch(error){showMessage(error.message,true)}
}
form.addEventListener('submit',event=>{event.preventDefault();key=new FormData(form).get('key');load()});
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-decision]');if(!button)return;
 if(!confirm('Confirm '+button.dataset.decision+'? Verify the professional qualification with its issuing body first.'))return;
 button.disabled=true;
 try{await api('/api/admin/reviews/'+encodeURIComponent(button.dataset.user)+'/decision',{method:'POST',body:JSON.stringify({decision:button.dataset.decision})});showMessage('Review decision saved.');await load()}
 catch(error){button.disabled=false;showMessage(error.message,true)}
});
})();
