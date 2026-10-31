(() => {
  const $=s=>document.querySelector(s);let csrf='';
  const status=$('#settings-status'),save=$('#save-settings');
  function render(value) {
    csrf=value.csrf;$('#assrt-state').textContent=value.assrt_configured?'已配置 · 查询时验证':'未配置';$('#opensubtitles-state').textContent=value.opensubtitles_configured?'已配置 · 查询时验证':'未配置';$('#subtitle-language').value=value.language || 'zh-CN';
  }
  async function request(options) {
    const response=await fetch('api/subtitles/settings',{...options,cache:'no-store'});const value=await response.json();if(!response.ok || !value.ok)throw new Error(value.error || '设置请求失败。');return value;
  }
  async function load() {try {render(await request());save.disabled=false;status.textContent='配置就绪。未填写的密钥将保持原值。';}catch(e){status.textContent=e.message;status.classList.add('error');}}
  $('#subtitle-settings').addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;status.classList.remove('error');status.textContent='正在保存…';
    const data={assrt_token:$('#assrt-token').value.trim(),opensubtitles_api_key:$('#opensubtitles-key').value.trim(),clear_assrt_token:$('#clear-assrt').checked,clear_opensubtitles_api_key:$('#clear-opensubtitles').checked,language:$('#subtitle-language').value};
    try {render(await request({method:'POST',headers:{'Content-Type':'application/json','X-Subtitle-CSRF':csrf},body:JSON.stringify(data)}));$('#assrt-token').value='';$('#opensubtitles-key').value='';$('#clear-assrt').checked=false;$('#clear-opensubtitles').checked=false;status.textContent='设置已保存。搜索字幕时会验证来源连接和密钥。';}catch(e){status.textContent=e.message;status.classList.add('error');}finally{save.disabled=false;}
  });load();
})();
