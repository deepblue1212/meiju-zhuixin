/* One shared subtitle sheet; credentials remain in NAS private settings. */
const SubtitleUI = (() => {
  const make = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text) el.textContent = text; return el; };
  const sheet = make('dialog', 'subtitle-sheet');
  sheet.setAttribute('aria-labelledby', 'subtitle-sheet-title');
  const head = make('div', 'sheet-head');
  const heading = make('h2', '', '中文字幕'); heading.id = 'subtitle-sheet-title';
  const close = make('button', 'secondary', '关闭'); close.type = 'button';
  head.append(heading, close);
  const release = make('p', 'sheet-release');
  const naming = make('label', 'sheet-field', '实际视频文件名');
  const filename = make('input'); filename.type = 'text'; filename.maxLength = 240; filename.placeholder = '留空使用字幕原名，填写后自动同名保存';
  naming.append(filename);
  const nameTip = make('p', 'hint', '填写下载后的真实视频文件名，或选择本地视频取名。只改字幕名称，不上传视频。');
  const pickerLabel = make('label', 'sheet-field compact-field', '选择本地视频取名');
  const picker = make('input'); picker.type = 'file'; picker.accept = '.mkv,.mp4,.avi,.mov,.wmv,.m4v,.ts,.m2ts,.webm'; pickerLabel.append(picker);
  const skipLabel = make('label', 'subtitle-skip'); const skip = make('input'); skip.type = 'checkbox';
  skipLabel.append(skip, document.createTextNode('已有中文字幕，跳过自动查找'));
  const hint = make('p', 'hint');
  const controls = make('div', 'actions');
  const automatic = make('button', '', '自动匹配并下载'); automatic.type = 'button';
  const search = make('button', 'secondary', '重新查找'); search.type = 'button';
  const backup = make('button', 'secondary', '换用备源'); backup.type = 'button';
  const settings = make('a', '', '字幕设置'); settings.href = 'settings.html';
  const subhd = make('a', '', '去 SubHD 下载'); subhd.target = '_blank'; subhd.rel = 'noopener noreferrer';
  controls.append(automatic, search, backup, settings, subhd);
  const status = make('p', 'sheet-status'); status.setAttribute('role', 'status');
  const sources = make('div', 'provider-status');
  const list = make('div', 'subtitle-options');
  const files = make('div', 'subtitle-files');
  const local = make('details', 'subtitle-rename'); local.append(make('summary', '', '已有字幕文件？同名另存'));
  const localLabel = make('label', '', '选择已下载的字幕'); const localFile = make('input'); localFile.type = 'file'; localFile.accept = '.srt,.ass,.ssa,.vtt'; localLabel.append(localFile);
  const langLabel = make('label', '', '本地字幕语言（仅用于命名，不转换内容）'); const language = make('select');
  [['zh-CN', '简体中文'], ['zh-TW', '繁体中文']].forEach(([v,t]) => {const o = make('option','',t);o.value=v;language.append(o);}); langLabel.append(language);
  const localSave = make('button', 'secondary', '保存同名字幕'); localSave.type = 'button';
  local.append(localLabel, langLabel, localSave);
  const footer = make('p', 'hint', '字幕保存到浏览器下载目录；请放到 NAS 视频旁。字幕源：ASSRT · OpenSubtitles。SubHD 提供网站入口。');
  const attribution=make('a','','字幕服务由 ASSRT 提供');attribution.href='https://assrt.net/';attribution.target='_blank';attribution.rel='noopener noreferrer';footer.append(document.createTextNode(' '),attribution);
  sheet.append(head, release, naming, nameTip, pickerLabel, skipLabel, hint, controls, status, sources, list, files, local, footer);
  document.body.append(sheet);
  let active = null, generation = 0, candidates = null, running = false, csrf = '';
  const controllers = new Set();
  function alive(id) { return sheet.open && generation === id; }
  function state(text, error = false) { status.textContent = text; status.classList.toggle('error', error); }
  function busy(value) { running = value; automatic.disabled = value; search.disabled = value; backup.disabled=value; list.querySelectorAll('button').forEach(b => b.disabled=value); files.querySelectorAll('button').forEach(b=>b.disabled=value); }
  async function api(path, options = {}) {
    const controller = new AbortController(); controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(path, {...options, signal:controller.signal, cache:'no-store'});
      if (!response.ok) { let message='字幕请求失败，请稍后重试。'; try { message=(await response.json()).error || message; } catch {} throw new Error(message); }
      return response;
    } catch(error) { if (error.name === 'AbortError') throw new Error('字幕查询超时或已取消，请重试。'); throw error; }
    finally {clearTimeout(timer);controllers.delete(controller);}
  }
  async function getConfig() { const value=await (await api('api/subtitles/settings')).json(); csrf=value.csrf; return value; }
  function clearRequests() { controllers.forEach(c=>c.abort());controllers.clear(); }
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob); const link=make('a'); link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  function attachmentName(response) {
    const match=(response.headers.get('Content-Disposition') || '').match(/filename\*=UTF-8''([^;]+)/i);
    if (!match) throw new Error('字幕响应缺少有效文件名，请重新查找。');
    try {return decodeURIComponent(match[1]);} catch {throw new Error('字幕文件名无法读取。');}
  }
  async function download(file, id) {
    if (!alive(id)) return;
    busy(true); state('正在获取字幕文件…');
    try {
      if (!csrf) await getConfig();
      if (!alive(id)) return;
      const response=await api('api/subtitles/download', {method:'POST', headers:{'Content-Type':'application/json','X-Subtitle-CSRF':csrf}, body:JSON.stringify({file_id:file.id, video_name:filename.value.trim(), language:file.language || language.value})});
      const name=attachmentName(response); const blob=await response.blob();
      if (!alive(id)) return;
      if (!blob.size) throw new Error('字幕文件为空，请换一个版本。');
      saveBlob(blob,name);state(`已发起下载：${name}。请将字幕放到视频同一目录，并播放确认同步。`);
      if(active)active.label.textContent='中文字幕 · 已发起下载 · 查看或更换';
    } catch(error) {if(alive(id))state(error.message,true);}
    finally {if(alive(id))busy(false);}
  }
  async function choose(candidate, id) {
    if (!alive(id) || running) return;
    busy(true); files.replaceChildren();state('正在读取可下载的字幕文件…');
    let single=null;
    try {
      const payload=await (await api(`api/subtitles/files?id=${encodeURIComponent(candidate.id)}`)).json();
      if (!alive(id)) return;
      if (!payload.files?.length) {
        state(payload.message || '此字幕需要到来源网站下载并解压。',true);
        try {const url=new URL(payload.websiteUrl);if(['https://assrt.net','https://www.opensubtitles.com'].includes(url.origin) && !url.username && !url.password){const link=make('a','','到字幕来源网站处理');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';files.append(link);}}catch{}
        return;
      }
      if(payload.files.length===1)single=payload.files[0];
      else {
        files.append(make('h3','','请选择对应集数和语言的字幕文件'));
        for(const file of payload.files) {
          const row=make('div','subtitle-file'); row.append(make('span','',file.name),make('small','',`${file.language || '语言待核对'} · ${file.format || ''}`));
          const b=make('button','secondary','下载此文件');b.type='button';b.addEventListener('click',()=>download(file,id));row.append(b);files.append(row);
        }
        state('此条目包含多个文件，请按实际季集和语言选择。');
      }
    } catch(error) {if(alive(id))state(error.message,true);}
    finally {if(alive(id))busy(false);}
    if(single && alive(id))await download(single,id);
  }
  async function lookup(autoDownload = false, backupOnly = false) {
    if(!active || running)return;
    const id=generation;busy(true);state('正在匹配中文字幕，ASSRT 优先…');files.replaceChildren();
    let best=null;
    try {
      const params=new URLSearchParams({title:active.item.title,zh:active.item.chineseTitle || ''});
      if(backupOnly)params.set('source','backup');
      const payload=await (await api(`api/subtitles?${params}`)).json();
      if(!alive(id))return;
      candidates=payload.items || [];list.replaceChildren();sources.replaceChildren();
      for(const provider of payload.providers || []) sources.append(make('p','',`${provider.id==='assrt'?'ASSRT':'OpenSubtitles'} · ${provider.message || provider.status}`));
      state(payload.message || (candidates.length?'请核对版本后下载。':'暂未找到中文字幕。'),['unavailable','unconfigured'].includes(payload.status));
      active.label.textContent=candidates.length?`中文字幕 · ${candidates.length} 个候选 · 查看下载`:'中文字幕 · 暂无可下载候选 · 查看详情';
      function renderCandidate(candidate) {
        const row=make('div','subtitle-option');row.append(make('strong','',candidate.title),make('small','',[candidate.provider==='assrt'?'ASSRT':'OpenSubtitles',candidate.matchLabel,...(candidate.languages||[]),...(candidate.formats||[]),...(candidate.reasons||[])].filter(Boolean).join(' · ')));
        const button=make('button','','下载字幕');button.type='button';button.addEventListener('click',()=>choose(candidate,id));row.append(button);list.append(row);
      }
      candidates.slice(0,3).forEach(renderCandidate);
      if(candidates.length>3){const more=make('button','secondary',`显示其余 ${candidates.length-3} 条`);more.type='button';more.addEventListener('click',()=>{more.remove();candidates.slice(3).forEach(renderCandidate);});list.append(more);}
      if(autoDownload) {
        const exact=candidates.filter(c=>c.autoMatch);
        if(exact.length===1)best=exact[0];
        else if(candidates.length)state('已找到候选，但无法唯一确认发布版本。请选一条下载，避免自动配错。');
      }
    } catch(error) {if(alive(id))state(error.message,true);}
    finally {if(alive(id))busy(false);}
    if(best && alive(id))await choose(best,id);
  }
  function open(item,label) {
    clearRequests();generation++;active={item,label};candidates=null;running=false;csrf='';
    release.textContent=item.title;filename.value='';picker.value='';localFile.value='';list.replaceChildren();files.replaceChildren();sources.replaceChildren();state('');
    skip.checked=subtitlePreference(item);
    hint.textContent=hasChineseSubtitleHint(item.title)?'片源标注中字，尚未检测视频内容；可以仍然查找。':'仅凭磁力链接无法确认视频内嵌字幕；已经确认有中文字幕时可以跳过。';
    subhd.href=`https://subhd.tv/search/${encodeURIComponent(item.releaseTitle || item.title)}`;
    busy(false);sheet.showModal();
    if(skip.checked)state('已按片源标注或你的选择跳过。需要补字幕时可点击“自动匹配并下载”。');else lookup(false);
  }
  close.addEventListener('click',()=>sheet.close());
  sheet.addEventListener('close',()=>{generation++;clearRequests();active=null;running=false;});
  picker.addEventListener('change',()=>{if(picker.files[0])filename.value=picker.files[0].name;});
  skip.addEventListener('change',()=>{if(active)saveSubtitlePreference(active.item,skip.checked);});
  search.addEventListener('click',()=>lookup(false));automatic.addEventListener('click',()=>lookup(true));
  backup.addEventListener('click',()=>lookup(false,true));
  localSave.addEventListener('click',()=>{try {if(!localFile.files[0])throw new Error('请先选择字幕文件。');const name=renamedSubtitle(filename.value,localFile.files[0].name,language.value);saveBlob(localFile.files[0],name);state(`已发起另存：${name}。`);}catch(e){state(e.message,true);}});
  return {panel(item) {const panel=make('div','subtitle-panel');const label=make('button','subtitle-launch',subtitlePreference(item)?'中文字幕 · 片源标注或手动跳过 · 仍可查找':'中文字幕 · 自动匹配与下载');label.type='button';label.addEventListener('click',()=>open(item,label));panel.append(label);return panel;}};
})();
