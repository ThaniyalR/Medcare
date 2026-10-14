(()=>{
'use strict';
const $=selector=>document.querySelector(selector);
const toastRoot=$('#toast-root');
function toast(text,error=false){toastRoot.innerHTML='<div class="toast'+(error?' error':'')+'">'+escapeHtml(text)+'</div>';setTimeout(()=>toastRoot.replaceChildren(),3200)}
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function request(url,options={}){
 const response=await fetch(url,{credentials:'same-origin',...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
 const data=await response.json();
 if(!response.ok){if(response.status===401)location.replace('/');throw new Error(data.error||'Request failed')}
 return data;
}
function statusLabel(status){return status==='payment_pending'?'Payment processing':status==='paid'?'Finding your caregiver':status==='assigned'?'Caregiver confirmed':status==='cancelled'?'Cancelled':status}
function safeDate(value){const d=new Date(value);return Number.isNaN(d.getTime())?'Schedule to be confirmed':d.toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function bookingHtml(booking){
 return '<article class="booking"><div class="booking-top"><div><h3>'+escapeHtml(booking.service_title)+'</h3><p>'+escapeHtml(safeDate(booking.scheduled_at))+'</p></div><span class="status '+escapeHtml(booking.status)+'">'+escapeHtml(statusLabel(booking.status))+'</span></div><div class="booking-meta"><span class="privacy-lock">'+escapeHtml(booking.currency)+' · '+(Number(booking.amount_paise)/100).toLocaleString('en-IN',{style:'currency',currency:'INR'})+'</span></div></article>';
}
async function refresh(){
 try{
  const [me,data]=await Promise.all([request('/api/me'),request('/api/dashboard')]);
  if(me.user.role!=='patient'){location.replace(me.user.role==='nurse'||me.user.role==='caretaker'?'/caregiver-dashboard.html':'/');return}
  const name=me.user.name;
  $('#user-name').textContent=name;$('#user-avatar').textContent=name.split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
  $('#greeting').textContent='Welcome, '+name.split(/\s+/)[0]+'.';
  $('#account-status').textContent=me.user.phoneVerified?'Mobile verified':'Phone not verified';
  $('#account-phone').textContent=me.user.phone+(me.user.phoneVerified?' · verified':' · local demo');
  const visits=data.bookings||[];$('#booking-count').textContent=visits.length;$('#care-team').textContent=visits.filter(x=>x.status==='assigned').length;
  $('#bookings-list').innerHTML=visits.length?visits.map(bookingHtml).join(''):'<div class="empty-state">No care visits arranged yet. Book a visit when you’re ready.</div>';
  const profile=data.profile||{};
  for(const [key,value] of Object.entries(profile)){const input=document.querySelector('#profile-form [name="'+key+'"]');if(input)input.value=value||''}
 }catch(error){showMessage(error.message)}
}
function showMessage(text){const box=$('#dashboard-message');box.hidden=false;box.classList.add('error');box.textContent=text}
async function openBooking(){
 const config=await request('/api/config');
 if(!config.razorpayKeyId){toast('Online booking needs the Nutrix administrator to configure Razorpay.',true);return}
 const future=new Date(Date.now()+2*3600000);future.setMinutes(Math.ceil(future.getMinutes()/15)*15,0,0);
 const localDate=new Date(future.getTime()-future.getTimezoneOffset()*60000).toISOString().slice(0,16);
 $('#modal-root').innerHTML='<div class="modal-back" data-backdrop><section class="modal" role="dialog" aria-modal="true" aria-labelledby="booking-title"><h2 id="booking-title">Arrange care</h2><p class="modal-sub">Choose a service and a convenient time. Your card details are handled by Razorpay, not stored by Nutrix.</p><form id="booking-form"><label class="field"><span>Care service</span><select name="plan"><option value="caregiver-4h">Care assistant · 4 hours — ₹1,200</option><option value="caregiver-8h">Care assistant · 8 hours — ₹2,200</option><option value="nurse-visit">Registered nurse · home visit — ₹900</option></select></label><label class="field"><span>Preferred date and time</span><input type="datetime-local" name="scheduledAt" min="'+localDate+'" value="'+localDate+'" required></label><p class="admin-warning">This booking is a request, not emergency care. In an emergency contact your local emergency service.</p><div class="modal-actions"><button class="button" type="button" data-modal-close>Cancel</button><button class="button primary" type="submit">Continue to secure payment →</button></div></form></section></div>';
 $('#booking-form').addEventListener('submit',submitBooking);
}
async function loadRazorpay(){
 if(window.Razorpay)return;
 await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://checkout.razorpay.com/v1/checkout.js';script.onload=resolve;script.onerror=()=>reject(new Error('Secure payment checkout could not load. Please try again.'));document.head.appendChild(script)});
}
async function submitBooking(event){
 event.preventDefault();const button=event.currentTarget.querySelector('[type="submit"]');button.disabled=true;
 try{
  await loadRazorpay();
  const fd=new FormData(event.currentTarget);
  const order=await request('/api/payments/order',{method:'POST',body:JSON.stringify({plan:fd.get('plan'),scheduledAt:fd.get('scheduledAt')})});
  const checkout=new window.Razorpay({key:order.keyId,amount:order.amount,currency:order.currency,name:'Nutrix Care',description:order.name,order_id:order.orderId,theme:{color:'#246d57'},handler:async response=>{
   try{await request('/api/payments/verify',{method:'POST',body:JSON.stringify(response)});$('#modal-root').replaceChildren();toast('Payment confirmed. Your care request is in the matching queue.');await refresh()}
   catch(error){toast('Payment confirmation needs a retry: '+error.message,true)}
  },modal:{ondismiss:()=>{button.disabled=false}},notes:{booking_id:order.bookingId}});
  checkout.on('payment.failed',response=>toast(response.error?.description||'Payment did not complete.',true));
  checkout.open();
 }catch(error){button.disabled=false;toast(error.message,true)}
}
$('#profile-form').addEventListener('submit',async event=>{
 event.preventDefault();const button=event.currentTarget.querySelector('[type="submit"]');button.disabled=true;
 const data=Object.fromEntries(new FormData(event.currentTarget).entries());
 try{const result=await request('/api/patient/profile',{method:'POST',body:JSON.stringify(data)});toast(result.message)}
 catch(error){toast(error.message,true)}finally{button.disabled=false}
});
document.addEventListener('click',event=>{
 if(event.target.closest('[data-logout]'))request('/api/auth/logout',{method:'POST',body:'{}'}).finally(()=>location.replace('/'));
 if(event.target.closest('#book-care')||event.target.closest('#book-care-secondary'))openBooking().catch(error=>toast(error.message,true));
 if(event.target.matches('[data-modal-close]')||event.target.matches('[data-backdrop]'))$('#modal-root').replaceChildren();
});
refresh();
})();
