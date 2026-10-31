/* fnOS desktop bridge. No credentials are read or stored. */
const fnDownload = (() => {
  const pending = new Map();
  let sequence = 0, methods = new Set(), ready = false;
  const origin = location.origin;
  let resolveReady, rejectReady;
  const connected = new Promise((resolve, reject) => {resolveReady = resolve; rejectReady = reject;});
  connected.catch(() => {});
  const timer = setTimeout(() => { clearInterval(retry); rejectReady(new Error('请从飞牛桌面打开本应用后使用下载联动。')); }, 15000);
  function send(message) { window.parent.postMessage(message, origin); }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== origin || !event.data) return;
    const data = event.data;
    if (data.penpal === 'synAck' && !ready && Array.isArray(data.methodNames)) {
      methods = new Set(data.methodNames);
      send({penpal:'ack', methodNames:[], config:{version:'1.9.2',postmateVersion:'0.1.2'}});
      ready = true; clearTimeout(timer); clearInterval(retry); resolveReady();
    } else if (data.penpal === 'reply' && pending.has(data.id)) {
      const task = pending.get(data.id); clearTimeout(task.timer); pending.delete(data.id);
      if (data.resolution === 'fulfilled') task.resolve(data.returnValue);
      else task.reject(new Error(data.returnValue?.message || '飞牛操作失败'));
    }
  });
  if (window.parent !== window) send({penpal:'syn'});
  const retry = setInterval(() => { if (!ready && window.parent !== window) send({penpal:'syn'}); }, 500);
  async function call(methodName, args, timeout=12000) {
    await connected;
    if (!methods.has(methodName)) throw new Error('当前飞牛版本未提供下载联动接口，请使用复制链接。');
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => {pending.delete(id); reject(new Error('飞牛响应超时。请先查看下载列表确认是否已添加，再决定重试。'));}, timeout);
      pending.set(id,{resolve,reject,timer}); send({penpal:'call',id,methodName,args});
    });
  }
  async function query(req, values={}) {
    const reqid = await call('genReqId',[]);
    const result = await call('query',[{req,reqid,...values},{timeout:15000}],18000);
    if (result?.result !== 'succ') throw new Error(result?.errmsg || result?.data?.errmsg || '飞牛拒绝了请求，请检查登录状态和下载权限。');
    return result?.data?.rsp;
  }
  function validMagnet(value) {
    if (typeof value !== 'string' || value.length > 16000) return false;
    try {const url = new URL(value); return url.protocol === 'magnet:' && url.searchParams.getAll('xt').some(x=>/^urn:btih:(?:[a-f0-9]{40}|[a-z2-7]{32})$/i.test(x));} catch {return false;}
  }
  return {
    connected,
    async directory() {const path=await query('appcgi.downloadcenter.config.getDefaultSaveDir'); if(typeof path !== 'string' || !path) throw new Error('请先在飞牛下载中设置默认存储目录。'); return path;},
    async add(magnets) {
      if (!Array.isArray(magnets) || !magnets.length || magnets.length > 50 || !magnets.every(validMagnet)) throw new Error('一次请选择 1–50 条有效磁力链接。');
      const unique=[...new Set(magnets)];
      const response=await query('appcgi.downloadcenter.task.addUris',{uris:unique,select_files:false});
      if (!Array.isArray(response?.ids) || response.ids.length !== unique.length || !response.ids.every(x=>Number.isInteger(x))) throw new Error('任务已提交，但返回结果无法确认。请先检查飞牛下载列表，勿重复添加。');
      return {created:response.ids.filter(x=>x!==-1).length,skipped:response.ids.filter(x=>x===-1).length};
    },
    async open() {await call('openApp',['trim.download-center']);}
  };
})();
