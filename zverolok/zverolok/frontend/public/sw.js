const CACHE='zverolok-shell-v1';
const SHELL=['/','/manifest.json','/icons/icon-192.png','/icons/icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(Promise.all([caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))),self.clients.claim()]))});
self.addEventListener('fetch',e=>{
 if(e.request.method!=='GET'||new URL(e.request.url).origin!==self.location.origin||new URL(e.request.url).pathname.startsWith('/api/'))return;
 if(e.request.mode==='navigate')e.respondWith(fetch(e.request).catch(()=>caches.match('/')));
 else e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request).then(res=>{if(res.ok){const copy=res.clone();caches.open(CACHE).then(c=>c.put(e.request,copy))}return res})));
});
self.addEventListener('push',e=>{
 let data={};try{data=e.data?.json()||{}}catch{data={body:e.data?.text()}}
 e.waitUntil(self.registration.showNotification(data.title||'ЗвероЛок',{body:data.body||'Новое объявление рядом',icon:'/icons/icon-192.png',badge:'/icons/icon-192.png',tag:'zverolok-alert',data:{url:data.url||'/'},vibrate:[200,100,200]}));
});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(async clients=>{const url=new URL(e.notification.data?.url||'/',self.location.origin).href;const existing=clients.find(c=>c.url===url);if(existing)return existing.focus();return self.clients.openWindow(url)}))});
