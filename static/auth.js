(()=>{
'use strict';
const $=selector=>document.querySelector(selector);
const message=$('#auth-message');
let mode='login',pendingPhone='';
let phoneVerificationRequired=true;
function showMessage(text,error=false){message.textContent=text;message.className='message'+(error?' error':'');message.hidden=false}
function clearMessage(){message.hidden=true;message.textContent=''}
function normalizePhone(value){return value.trim().replace(/[()\s-]/g,'')}
function destination(role){return role==='patient'?'/patient-dashboard.html':'/caregiver-dashboard.html'}
async function api(path,body){
 const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify(body)});
 const data=await response.json();
 if(!response.ok)throw new Error(data.error||'Something went wrong. Please try again.');
 return data;
}
function setMode(next){
 mode=next;clearMessage();
 document.querySelectorAll('[data-auth-mode]').forEach(button=>{const active=button.dataset.authMode===mode;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
 $('#login-form').hidden=mode!=='login';$('#register-form').hidden=mode!=='register';
 $('#form-title').textContent=mode==='login'?'Good to have you back.':'Let’s get you started.';
 $('#form-subtitle').textContent=mode==='login'?'Sign in to your secure care workspace.':'First, tell us a little about yourself.';
}
function toggleProfessionalFields(){
 const role=$('#signup-role').value,fields=$('#professional-fields'),active=role==='nurse'||role==='caretaker';
 fields.hidden=!active;
 fields.querySelector('[name="credentialType"]').required=active;
 fields.querySelector('[name="credentialId"]').required=active;
}
async function loadAuthConfig(){
 try{
  const response=await fetch('/api/config',{credentials:'same-origin'});
  const config=await response.json();
  if(!response.ok)throw new Error(config.error||'Could not load account settings.');
  phoneVerificationRequired=config.phoneVerificationRequired!==false;
  const phoneHelp=$('#register-form [name="phone"]').nextElementSibling;
  phoneHelp.textContent=phoneVerificationRequired
   ?'Use international format with digits only, so we can send your SMS code.'
   :'Phone verification is disabled in this local demo; use a number you control.';
  $('#register-form [type="submit"]').innerHTML=phoneVerificationRequired?'Continue with mobile verification <span>→</span>':'Create account <span>→</span>';
  $('.security-foot').innerHTML=phoneVerificationRequired
   ?'<span>◈</span> Your account is protected with verified mobile sign-in.'
   :'<span>◈</span> Phone numbers are not verified in this local development mode.';
 }catch(error){showMessage(error.message,true)}
}
document.querySelectorAll('[data-auth-mode]').forEach(button=>button.addEventListener('click',()=>setMode(button.dataset.authMode)));
$('#signup-role').addEventListener('change',toggleProfessionalFields);
toggleProfessionalFields();
loadAuthConfig();
$('#login-form').addEventListener('submit',async event=>{
 event.preventDefault();clearMessage();
 const submit=event.currentTarget.querySelector('[type="submit"]');submit.disabled=true;
 const fd=new FormData(event.currentTarget);
 try{
  const result=await api('/api/auth/login',{phone:normalizePhone(fd.get('phone')),password:fd.get('password')});
  window.location.assign(destination(result.user.role));
 }catch(error){showMessage(error.message,true)}finally{submit.disabled=false}
});
$('#register-form').addEventListener('submit',async event=>{
 event.preventDefault();clearMessage();
 const submit=event.currentTarget.querySelector('[type="submit"]');submit.disabled=true;
 const fd=new FormData(event.currentTarget);pendingPhone=normalizePhone(fd.get('phone'));
 const payload={role:fd.get('role'),name:String(fd.get('name')).trim(),phone:pendingPhone,password:fd.get('password'),credentialType:fd.get('credentialType'),credentialId:fd.get('credentialId'),privacyConsent:fd.get('consent')==='on'};
 try{
  const result=await api('/api/auth/register',payload);
  if(result.user){window.location.assign(destination(result.user.role));return}
  $('#otp-phone').textContent=pendingPhone;$('#auth-forms').hidden=true;$('#otp-panel').hidden=false;clearMessage();
  $('#otp-form [name="code"]').focus();
 }catch(error){showMessage(error.message,true)}finally{submit.disabled=false}
});
$('#otp-form').addEventListener('submit',async event=>{
 event.preventDefault();clearMessage();
 const submit=event.currentTarget.querySelector('[type="submit"]');submit.disabled=true;
 const code=new FormData(event.currentTarget).get('code');
 try{
  const result=await api('/api/auth/verify',{phone:pendingPhone,code});
  window.location.assign(destination(result.user.role));
 }catch(error){showMessage(error.message,true)}finally{submit.disabled=false}
});
$('#back-to-signup').addEventListener('click',()=>{$('#otp-panel').hidden=true;$('#auth-forms').hidden=false;setMode('register')});
})();
