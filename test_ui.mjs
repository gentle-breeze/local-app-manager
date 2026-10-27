import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.checked=false;this.disabled=false;this.open=false;this.ownText='';}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(c=>c.textContent).join('');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;this.ownText='';}
 showModal(){this.open=true;}
 close(){this.open=false;}
}
const html=fs.readFileSync('web/index.html','utf8');
const ids=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([,id])=>[id,new Element()]));
const form=ids.form;
form.elements=Object.fromEntries([...html.matchAll(/name="([^"]+)"/g)].map(([,name])=>[name,new Element()]));
for(const key of ['stop_signal','stop_timeout','stop_policy','github_url','github_branch','build_argv'])assert.ok(form.elements[key],`${key} present in HTML`);
const save=new Element('button');form.querySelector=()=>save;
form.reset=()=>{for(const e of Object.values(form.elements)){e.value='';e.checked=false;}form.elements.stop_signal.value='SIGTERM';form.elements.stop_timeout.value=10;form.elements.stop_policy.value='kill';};
const fixture={id:'synthetic',name:'test',port:0,readonly:false,managed:true,lifecycle:'stop_timeout',lifecycle_error:'Synthetic stop timed out',logs:'',cpu:null,cwd:'/tmp',argv:['/usr/bin/python3']};
const requests=[];
const context=vm.createContext({document:{querySelector:s=>ids[s.slice(1)],createElement:t=>new Element(t)},fetch:async(path,opts)=>{if(opts){requests.push({path,body:JSON.parse(opts.body)});return{ok:path!='/api/action',json:async()=>path==='/api/action'?{error:'Synthetic stop timed out'}:{ok:true}};}return{ok:true,json:async()=>({token:'fake',apps:[fixture],sampled:0})};},setInterval:()=>0});
vm.runInContext(fs.readFileSync('web/app.js','utf8'),context);
await vm.runInContext('refresh()',context);
vm.runInContext('openEditor()',context);
assert.equal(form.elements.stop_signal.value,'SIGTERM');assert.equal(form.elements.stop_timeout.value,10);assert.equal(form.elements.stop_policy.value,'kill');
vm.runInContext('openEditor({id:"existing", name:"legacy", port:0, readonly:false})',context);
assert.equal(form.elements.stop_signal.value,'SIGTERM');assert.equal(form.elements.stop_timeout.value,10);assert.equal(form.elements.stop_policy.value,'kill');
vm.runInContext('openEditor({id:"existing", name:"custom", cwd:"/tmp", port:0, readonly:false, stop_signal:"SIGINT", stop_timeout:0.25, stop_policy:"wait"})',context);
assert.equal(form.elements.stop_signal.value,'SIGINT');assert.equal(form.elements.stop_timeout.value,0.25);assert.equal(form.elements.stop_policy.value,'wait');
await Promise.all([form.onsubmit({preventDefault(){},target:form}),form.onsubmit({preventDefault(){},target:form})]);
assert.equal(requests.length,1,'double submit saves once');
assert.equal(requests[0].body.id,'existing');assert.equal(requests[0].body.stop_signal,'SIGINT');assert.equal(requests[0].body.stop_timeout,0.25);assert.equal(requests[0].body.stop_policy,'wait');
const all=node=>[node,...node.children.flatMap(all)];
const button=label=>all(ids.apps).find(n=>n.tag==='button'&&n.textContent===label);
assert.equal(button('启动').disabled,true);assert.equal(button('设置').disabled,true);assert.equal(button('停止').disabled,false);assert.match(ids.apps.textContent,/停止超时 · 尚未退出/);assert.match(ids.apps.textContent,/Synthetic stop timed out/);
await button('重启').onclick();assert.equal(ids.error.textContent,'Synthetic stop timed out');
await vm.runInContext('refresh()',context);assert.equal(ids.error.textContent,'Synthetic stop timed out','poll does not erase action timeout');
vm.runInContext('openEditor()',context);assert.equal(form.elements.stop_policy.value,'kill','new dialog resets previous policy');
vm.runInContext('openEditor({id:"remote",name:"remote",cwd:"/tmp",port:0,readonly:false,github_url:"https://github.com/example/project",github_branch:"release/stable",build_argv:["/usr/bin/python3","build.py"]})',context);
assert.equal(form.elements.github_url.value,'https://github.com/example/project');
assert.equal(form.elements.build_argv.value,'["/usr/bin/python3","build.py"]');
await form.onsubmit({preventDefault(){},target:form});
assert.equal(requests.at(-1).body.github_url,'https://github.com/example/project');
assert.equal(form.elements.github_branch.value,'release/stable');
assert.equal(requests.at(-1).body.github_branch,'release/stable');
assert.deepEqual(requests.at(-1).body.build_argv,['/usr/bin/python3','build.py']);
vm.runInContext('openEditor()',context);
assert.equal(form.elements.github_url.value,'');
assert.equal(form.elements.github_branch.value,'main');
assert.equal(form.elements.build_argv.value,'');
const beforeInvalid=requests.length;
form.elements.build_argv.value='not json';
await form.onsubmit({preventDefault(){},target:form});
assert.equal(requests.length,beforeInvalid,'invalid build JSON cannot be submitted');
assert.ok(ids.formerror.textContent);
assert.equal(save.disabled,false,'invalid JSON re-enables save');
Object.assign(fixture,{managed:false,lifecycle:'stopped',lifecycle_error:'',github_url:'https://github.com/example/project',github_branch:'release/stable',deployment_phase:'fetching'});
await vm.runInContext('refresh()',context);
assert.equal(button('重启').disabled,true);
assert.equal(button('启动').disabled,true);
assert.equal(button('设置').disabled,true);
assert.match(ids.apps.textContent,/正在拉取配置分支/);
assert.match(ids.apps.textContent,/release\/stable/);
fixture.deployment_phase='building';
await vm.runInContext('refresh()',context);
assert.match(ids.apps.textContent,/正在构建配置分支/);
Object.assign(fixture,{deployment_phase:'error',deployment_error:'Synthetic fetch failed'});
await vm.runInContext('refresh()',context);
assert.equal(button('重启').disabled,false,'stopped remote app can retry configured branch deployment');
assert.equal(button('设置').disabled,false);
assert.match(ids.apps.textContent,/Synthetic fetch failed/);
Object.assign(fixture,{deployment_phase:'ready',deployment_error:'',deployed_commit:'abc1234567',deployed_cwd:'/tmp/releases/revision'});
await vm.runInContext('refresh()',context);
assert.match(ids.apps.textContent,/abc1234567/);
assert.match(ids.apps.textContent,/\/tmp\/releases\/revision/);
fixture.github_url='';
await vm.runInContext('refresh()',context);
assert.equal(button('重启').disabled,true,'legacy stopped app preserves restart gating');
console.log('UI regression checks passed: config defaults/round-trip, reset, duplicate save, timeout button gating/status/error persistence.');
