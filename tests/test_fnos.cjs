const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(require('path').join(__dirname,'../app/public/fnos.js'),'utf8');
// Every landing page must handshake before the desktop's 60-second timeout.
for (const page of ['tv.html','index.html','settings.html']) {
 const html=fs.readFileSync(require('path').join(__dirname,'../app/public',page),'utf8');
 assert.match(html,/<script src="fnos\.js" defer><\/script>/,page+' must establish the desktop connection');
}
let listener, sends=[], requestCount=0;
const parent={postMessage(message){sends.push(message);if(message.penpal==='call')queueMicrotask(()=>{let value;if(message.methodName==='genReqId')value='test-'+(++requestCount);else if(message.methodName==='query'){let q=message.args[0];value={result:'succ',data:{rsp:q.req.endsWith('getDefaultSaveDir')?'/vol2/1000/movie':{ids:[12,-1]}}};}listener({source:parent,origin:'http://nas.test',data:{penpal:'reply',id:message.id,resolution:'fulfilled',returnValue:value}});});}};
const window={parent,addEventListener:(kind,fn)=>{listener=fn;}};
const ctx=vm.createContext({window,location:{origin:'http://nas.test'},URL,setTimeout,clearTimeout,setInterval,clearInterval,Error,Promise,Map,Set});vm.runInContext(code,ctx);
(async()=>{
 assert.equal(sends[0].penpal,'syn');
 listener({source:{},origin:'http://nas.test',data:{penpal:'synAck',methodNames:['query','genReqId']}});assert.equal(sends.length,1);
 listener({source:parent,origin:'http://evil.test',data:{penpal:'synAck',methodNames:['query','genReqId']}});assert.equal(sends.length,1);
 listener({source:parent,origin:'http://nas.test',data:{penpal:'synAck',methodNames:['query','genReqId','openApp']}});
 const api=vm.runInContext('fnDownload',ctx);assert.equal(await api.directory(),'/vol2/1000/movie');
 await assert.rejects(()=>api.add(['https://example.org']),/磁力/);
 const a='magnet:?xt=urn:btih:'+'a'.repeat(40),b='magnet:?xt=urn:btih:'+'b'.repeat(40);
 const r=await api.add([a,a,b]);assert.equal(r.created,1);assert.equal(r.skipped,1);
 const submission=sends.filter(x=>x.methodName==='query').at(-1).args[0];assert.equal(submission.uris.length,2);assert.equal(submission.select_files,false);
 await api.open();assert.equal(sends.at(-1).args[0],'trim.download-center');
 console.log('fnOS bridge tests passed: origin/source isolation, directory, validation, deduplication, counts, open');
})().catch(e=>{console.error(e);process.exitCode=1;});
