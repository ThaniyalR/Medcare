(()=>{
'use strict';
const $=selector=>document.querySelector(selector);
const toastRoot=$('#toast-root');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(text,error=false){toastRoot.innerHTML='<div class="toast">'+esc(text)+'</div>';setTimeout(()=>toastRoot.replaceChildren(),3300)}
async function request(url,options={}){
 const response=await fetch(url,{credentials:'same-origin',...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
 const data=await response.json();
 if(!response.ok){if(response.status===401)location.replace('/');throw new Error(data.error||'Request failed')}
 return data;
}
function displayDate(value){const date=new Date(value);return Number.isNaN(date.getTime())?'Schedule to be confirmed':date.toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function renderBooking(booking){
 const canAccept=booking.status==='paid',patient=booking.patient_name;
 const careDetails=patient?'<details class="care-details"><summary>Assigned patient care information</summary><div><p><strong>Conditions:</strong> '+esc(booking.patient_conditions||'Not provided')+'</p><p><strong>Allergies:</strong> '+esc(booking.patient_allergies||'Not provided')+'</p><p><strong>Mobility:</strong> '+esc(booking.patient_mobility||'Not provided')+'</p><p><strong>Care notes:</strong> '+esc(booking.patient_care_notes||'Not provided')+'</p><p><strong>Emergency contact:</strong> '+esc(booking.patient_emergency_contact||'Not provided')+'</p></div></details>':'<p class="card-description" style="margin:7px 0 0">Patient details become available only after you accept this request.</p>';
 return '<article class="booking"><div class="booking-top"><div><h3>'+esc(booking.service_title)+'</h3><p>'+esc(displayDate(booking.scheduled_at))+'</p></div><span class="status '+esc(booking.status)+'">'+(canAccept?'Open request':esc(booking.status==='assigned'?'Assigned to you':'Care visit'))+'</span></div>'+(patient?'<div class="booking-meta"><span class="privacy-lock">Patient: '+esc(patient)+'</span></div>':'<p class="card-description" style="margin:7px 0 0">Patient details become available only after you accept this request.</p>')+'<div class="booking-meta"><span class="privacy-lock">'+esc(booking.currency)+' · '+(Number(booking.amount_paise)/100).toLocaleString('en-IN',{style:'currency',currency:'INR'})+'</span></div>'+(patient?careDetails:'')+(canAccept?'<button class="button primary accept-button" style="margin-top:11px" data-accept="'+esc(booking.id)+'">Accept care request</button>':'')+'</article>';
}
function message(text){const box=$('#dashboard-message');box.hidden=false;box.classList.add('error');box.textContent=text}
async function refresh(){
 try{
  const me=await request('/api/me');const user=me.user;
  if(user.role==='patient'){location.replace('/patient-dashboard.html');return}
  if(user.role!=='nurse'&&user.role!=='caretaker'){location.replace('/');return}
  $('#user-name').textContent=user.name;$('#user-role').textContent=user.role==='nurse'?'Registered nurse':'Caretaker';$('#user-avatar').textContent=user.name.split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();$('#greeting').textContent='Welcome, '+user.name.split(/\s+/)[0]+'.';
  $('#review-status').textContent=user.reviewStatus==='approved'?'Approved':user.reviewStatus==='pending'?'In review':'Not approved';
  const phoneStatus=user.phoneVerified?'verified':'not verified (local demo)';
  $('#phone-status').textContent='Mobile '+user.phone+' · '+phoneStatus;
  $('#credential-details').innerHTML='<div class="credential-row"><span>Professional status</span><strong>'+esc(user.reviewStatus==='approved'?'Verified by Nutrix administrator':'Pending administrator review')+'</strong></div><div class="credential-row"><span>Credential type</span><strong>'+esc(user.credentialType||'Not supplied')+'</strong></div><div class="credential-row"><span>Registration / certificate</span><strong>'+esc(user.credentialId||'Not supplied')+'</strong></div><div class="credential-row"><span>Mobile number</span><strong>'+esc(user.phone)+' · '+esc(phoneStatus)+'</strong></div>';
  if(user.reviewStatus!=='approved'){
   $('#review-pending').hidden=false;
   $('#phone-review-status').textContent=user.phoneVerified?'Your mobile number is verified.':'Your mobile number is not verified in this local demo.';
   $('#credential-summary').textContent=(user.credentialType||'professional credential')+' ('+(user.credentialId||'number submitted')+')';
   $('#care-requests').innerHTML='<div class="empty-state">Your professional review is pending. Care requests will appear after your credentials are approved.</div>';
   $('#open-count').textContent='—';$('#assigned-count').textContent='—';return;
  }
  const result=await request('/api/dashboard'),bookings=result.bookings||[];
  $('#open-count').textContent=bookings.filter(x=>x.status==='paid').length;
  $('#assigned-count').textContent=bookings.filter(x=>x.status==='assigned').length;
  $('#care-requests').innerHTML=bookings.length?bookings.map(renderBooking).join(''):'<div class="empty-state">No matching care requests just yet. Check back soon.</div>';
 }catch(error){message(error.message)}
}
document.addEventListener('click',async event=>{
 if(event.target.closest('[data-logout]')){try{await request('/api/auth/logout',{method:'POST',body:'{}'})}finally{location.replace('/')}return}
 const button=event.target.closest('[data-accept]');if(!button)return;
 button.disabled=true;
 try{const result=await request('/api/bookings/'+encodeURIComponent(button.dataset.accept)+'/accept',{method:'POST',body:'{}'});toast(result.message);await refresh()}
 catch(error){button.disabled=false;toast(error.message,true)}
});
refresh();
})();
