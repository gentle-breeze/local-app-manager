// Explicitly requested isolated headless acceptance. Never attaches to desktop Chrome.
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, writeFile, readFile, rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import assert from 'node:assert/strict';
import net from 'node:net';
import {setTimeout as delay} from 'node:timers/promises';
const root=path.dirname(new URL(import.meta.url).pathname);
const tmp=await mkdtemp(path.join(os.tmpdir(),'app-manager-headless-'));
const evidence=path.join(root,'evidence');await mkdir(evidence,{recursive:true});
const checks=[],errors=[];let manager,chrome,ws;
async function freePort(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function until(fn){for(let i=0;i<120;i++){try{if(await fn())return;}catch{}await delay(100);}throw Error('Timed out waiting for test condition');}
async function stop(child){if(!child||child.exitCode!==null||child.signalCode!==null)return;const ended=new Promise(r=>child.once('exit',r));child.kill('SIGTERM');await Promise.race([ended,delay(6000)]);if(child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await ended;}}
try{
 const port=await freePort(), appPort=await freePort(), downPort=await freePort();
 const data=path.join(tmp,'data');await mkdir(data);await writeFile(path.join(data,'apps.json'),'[]');
 const log=path.join(tmp,'synthetic.log');await writeFile(log,'INFO synthetic fixture ready\nTOKEN=synthetic-private-value\nERROR synthetic example only\n');
 manager=spawn('/usr/bin/python3',[path.join(root,'server.py'),'--port',String(port),'--data-dir',data],{stdio:'ignore'});
 const url=`http://127.0.0.1:${port}`;
 await until(async()=> (await fetch(url)).ok);
 chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--no-first-run','--no-default-browser-check','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',`--user-data-dir=${path.join(tmp,'profile')}`,'about:blank'],{stdio:'ignore'});
 let debugPort;await until(async()=>{debugPort=(await readFile(path.join(tmp,'profile','DevToolsActivePort'),'utf8')).split('\n')[0];return debugPort;});
 const targets=await(await fetch(`http://127.0.0.1:${debugPort}/json`)).json();ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
 let serial=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);if(pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++serial;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},10000);pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
 const click=selector=>evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
 const fill=fields=>evaluate(`(()=>{const f=document.querySelector('#form');for(const [k,v] of Object.entries(${JSON.stringify(fields)})){if(k==='managed')f.elements[k].checked=v;else f.elements[k].value=v;}})()`);
 const submit=()=>evaluate(`document.querySelector('#form').requestSubmit()`);
 const text=()=>evaluate('document.body.innerText');
 const count=()=>evaluate("document.querySelectorAll('#apps article').length");
 const shot=async name=>{const r=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await writeFile(path.join(evidence,name),Buffer.from(r.data,'base64'));};
 const button=async(name,label)=>evaluate(`(()=>{const c=[...document.querySelectorAll('article')].find(c=>c.querySelector('h3').textContent===${JSON.stringify(name)});const b=[...c.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify(label)});if(!b||b.disabled)throw Error('Button unavailable');b.click();})()`);
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 const navigation=await cdp('Page.navigate',{url});if(navigation.errorText)throw Error('Navigation blocked: '+navigation.errorText);
 await until(()=>evaluate("document.querySelector('#updated')?.textContent.startsWith('更新于')"));assert.equal(await count(),0);checks.push('Empty isolated dashboard loaded; no real app registrations');
 await click('#add');await fill({name:'CANCELLED',port:downPort});await click('#cancel');assert.equal(await count(),0);await click('#add');assert.equal(await evaluate("document.querySelector('#form').elements.name.value"),'');await evaluate("document.querySelector('#editor').close()");checks.push('Cancel and reopen discard unsaved fields');
 await click('#add');await fill({name:'离线观察示例',port:downPort});await submit();await until(async()=>await count()===1);assert.match(await text(),/未发现监听/);checks.push('Minimal read-only registration and unreachable state');
 await click('#add');await fill({name:'验收专用服务',port:appPort,cwd:tmp,health:'/',logpath:log,managed:true,argv:JSON.stringify(['/usr/bin/python3','-u','-m','http.server',String(appPort),'--bind','127.0.0.1'])});
 await evaluate("document.querySelector('#form').requestSubmit();document.querySelector('#form').requestSubmit()");await until(()=>evaluate("!document.querySelector('#editor').open"));await delay(500);assert.equal(await count(),2,'double submit must register one application');checks.push('Repeated save registers exactly one application');
 await button('验收专用服务','启动');await until(async()=>{await click('#refresh');return(await text()).includes('HTTP 200');});assert.match(await text(),/业务健康\n未知/);assert.match(await text(),/端口 \+ 工作目录已核实/);checks.push('Dedicated app starts; HTTP reachable distinct from business health; PID identity verified');
 assert.equal(await evaluate("[...document.querySelectorAll('article')].find(c=>c.querySelector('h3').textContent==='验收专用服务').querySelectorAll('button:disabled').length"),2);checks.push('Duplicate start and edit disabled while managed app runs');
 await shot('dashboard-wide.png');await button('验收专用服务','最近日志');assert.match(await evaluate("document.querySelector('#logtext').textContent"),/ERROR synthetic example only/);assert.doesNotMatch(await evaluate("document.querySelector('#logtext').textContent"),/synthetic-private-value/);await shot('logs.png');await click('#closelogs');checks.push('Log dialog displays fixture and redacts synthetic credential line');
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});assert.equal(await evaluate('window.innerWidth'),390,'mobile viewport must not auto-expand');assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true);await shot('dashboard-narrow.png');await click('#add');await shot('add-narrow.png');assert.equal(await evaluate("document.querySelector('#editor').getBoundingClientRect().right<=window.innerWidth"),true);await click('#cancel');assert.equal(await count(),2);checks.push('390px viewport has no horizontal overflow; dialog fits; cancel unchanged');
 await button('验收专用服务','重启');await until(async()=>{await click('#refresh');return(await text()).includes('HTTP 200');});await until(()=>evaluate("[...document.querySelectorAll('article')].find(c=>c.querySelector('h3').textContent==='验收专用服务').querySelectorAll('button:disabled').length===2"));await button('验收专用服务','停止');await until(async()=>{await click('#refresh');return(await evaluate("document.querySelector('#managed').textContent"))==='0';});checks.push('Dedicated app restart and stop work; no external process action');
 assert.deepEqual(errors,[]);checks.push('No JavaScript runtime exceptions');
 await writeFile(path.join(evidence,'headless-results.json'),JSON.stringify({passed:true,checks,errors,scope:'Independent headless Chrome; temporary profile/data; synthetic apps only. Not desktop acceptance.'},null,2));console.log(JSON.stringify({passed:true,checks,evidence},null,2));
}catch(error){await writeFile(path.join(evidence,'headless-results.json'),JSON.stringify({passed:false,checks,error:String(error)},null,2));throw error;}
finally{ws?.close();await stop(manager);await stop(chrome);await rm(tmp,{recursive:true,force:true});}
