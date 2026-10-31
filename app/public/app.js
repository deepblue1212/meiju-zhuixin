const $ = (selector) => document.querySelector(selector);
const form = $("#search-form");
const queryInput = $("#query");
const submitButton = form.querySelector('button[type="submit"]');
const statusBox = $("#status");
const optionsBox = $("#title-options");
const resultControls = $("#result-controls");
const toolbar = $("#toolbar");
const resultsBox = $("#results");
const selectAllButton = $("#select-all");
const copySelectedButton = $("#copy-selected");
const searchFilteredButton = $("#search-filtered");
let requestId = 0;
let selectedMatch = null;
let allResults = [];
let activeSearch = null;
const subtitleRequests = new Map();
const subtitleQueue = [];
let subtitleWorkers = 0;

function subtitleLink(label, value) {
  const link = document.createElement("a");
  try {
    const url = new URL(value);
    if (!["https://subhd.tv", "https://subhd.me", "https://subhd.one"].includes(url.origin) || url.username || !/^\/(?:search|a)\//.test(url.pathname)) return null;
    link.href = url.href;
  } catch { return null; }
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  return link;
}

function drainSubtitleQueue() {
  while (subtitleWorkers < 2 && subtitleQueue.length) {
    const task = subtitleQueue.shift();
    subtitleWorkers++;
    task().finally(() => { subtitleWorkers--; drainSubtitleQueue(); });
  }
}

function loadSubtitles(item) {
  const params = new URLSearchParams({title: item.title, zh: item.chineseTitle || ""});
  const key = params.toString();
  const cached = subtitleRequests.get(key);
  if (!cached || Date.now() - cached.created > 60000) {
    const pending = new Promise((resolve) => {
      subtitleQueue.push(async () => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 25000);
        try {
          const response = await fetch(`api/subtitles?${params}`, {signal: controller.signal, cache: "no-store"});
          if (!response.ok) throw new Error("字幕请求失败");
          const payload = await response.json();
          if (!payload.ok || !Array.isArray(payload.items)) throw new Error("字幕数据无效");
          resolve(payload);
        } catch {
          resolve({items: [], message: "未能自动读取字幕，请打开 SubHD 查找。"});
        } finally { clearTimeout(timeout); }
      });
    });
    subtitleRequests.set(key, {promise: pending, created: Date.now()});
    // Bound the in-page cache without duplicating requests still in flight.
    if (subtitleRequests.size > 200) subtitleRequests.delete(subtitleRequests.keys().next().value);
    drainSubtitleQueue();
  }
  return subtitleRequests.get(key).promise;
}

function hasChineseSubtitleHint(title) {
  return /(?:^|[^a-z])(?:chs|cht)(?:[^a-z]|$)|中字|中英双字|中文字幕/i.test(title);
}
function subtitlePreference(item) {
  try {
    const saved = JSON.parse(localStorage.getItem('subtitle-skips') || '{}');
    if (Object.prototype.hasOwnProperty.call(saved, item.magnet)) return saved[item.magnet] === true;
  } catch {}
  return hasChineseSubtitleHint(item.title);
}
function saveSubtitlePreference(item, value) {
  try {
    let saved = JSON.parse(localStorage.getItem('subtitle-skips') || '{}');
    if (!saved || Array.isArray(saved) || typeof saved !== 'object') saved = {};
    delete saved[item.magnet]; saved[item.magnet] = value;
    const entries = Object.entries(saved).slice(-200);
    localStorage.setItem('subtitle-skips', JSON.stringify(Object.fromEntries(entries)));
  } catch {}
}
function renamedSubtitle(video, subtitle, language) {
  const ext = subtitle.match(/\.(srt|ass|ssa|vtt)$/i);
  if (!ext) throw new Error('请选择 SRT、ASS、SSA 或 VTT 字幕；ZIP/RAR/7Z 请先解压。');
  const file = video.trim().split(/[\\/]/).pop();
  if (!file) throw new Error('请填写或选择实际下载的视频文件名。');
  const stem = file.replace(/\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts|webm|flv|mpg|mpeg)$/i, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '');
  if (!stem || stem.length > 200) throw new Error('视频文件名为空或过长，请调整后重试。');
  return `${stem}.${language === 'zh-TW' ? 'zh-TW' : 'zh-CN'}.${ext[1].toLowerCase()}`;
}
function createSubtitlePanel(item) {
  return SubtitleUI.panel(item);
}

const downloadDialog = document.createElement('dialog');
downloadDialog.className = 'download-dialog';
const downloadTitle = document.createElement('h2'); downloadTitle.textContent = '下载到飞牛';
const downloadNote = document.createElement('p');
const downloadPath = document.createElement('p'); downloadPath.className = 'download-path';
const downloadStatus = document.createElement('p'); downloadStatus.setAttribute('role','status');
const downloadActions = document.createElement('div'); downloadActions.className = 'actions';
const downloadCancel = document.createElement('button'); downloadCancel.type='button'; downloadCancel.textContent='关闭'; downloadCancel.className='secondary';
const downloadConfirm = document.createElement('button'); downloadConfirm.type='button'; downloadConfirm.textContent='确认添加';
const downloadOpen = document.createElement('button'); downloadOpen.type='button'; downloadOpen.textContent='查看飞牛下载'; downloadOpen.className='secondary';
downloadActions.append(downloadCancel,downloadConfirm,downloadOpen);
downloadDialog.append(downloadTitle,downloadNote,downloadPath,downloadStatus,downloadActions); document.body.append(downloadDialog);
let downloadItems=[], downloadBusy=false;
downloadCancel.addEventListener('click',()=>downloadDialog.close());
downloadDialog.addEventListener('cancel', event=>{if(downloadBusy)event.preventDefault();});
downloadOpen.addEventListener('click',async()=>{try {await fnDownload.open();} catch(error){downloadStatus.textContent=error.message;}});
async function prepareDownload(items) {
  if(downloadBusy) return;
  if(!items.length) return setStatus('请先选择要下载的资源。',true);
  if(items.length>50) return setStatus('每次最多添加 50 条，请减少选择。',true);
  downloadItems=items; downloadNote.textContent=items.length===1?items[0].title:`将添加 ${items.length} 条资源。请确认这些资源是你要下载的版本。`;
  downloadPath.textContent='正在读取飞牛默认下载目录…'; downloadStatus.textContent=''; downloadConfirm.disabled=true; downloadConfirm.textContent='确认添加'; downloadDialog.showModal();
  try {const dir=await fnDownload.directory(); if(!downloadDialog.open)return; downloadPath.textContent=`存储目录：${dir}（可在飞牛下载中更改）`; downloadConfirm.disabled=false;}
  catch(error){downloadPath.textContent='未连接到飞牛下载'; downloadStatus.textContent=error.message;}
}
downloadConfirm.addEventListener('click',async()=>{
  if(downloadBusy)return;
  downloadBusy=true;downloadConfirm.disabled=true;downloadCancel.disabled=true;downloadStatus.textContent='正在提交，请勿重复点击…';
  try {
    const result=await fnDownload.add(downloadItems.map(item=>item.magnet));
    downloadStatus.textContent=`已添加 ${result.created} 条${result.skipped?`，重复任务跳过 ${result.skipped} 条`:''}。可在飞牛下载中查看进度。`;
    downloadConfirm.textContent='已处理';
  }catch(error){downloadStatus.textContent=error.message;downloadConfirm.textContent='请先核对下载列表';}
  finally{downloadBusy=false;downloadCancel.disabled=false;}
});
$('#download-selected').addEventListener('click',()=>{const selected=new Set(Array.from(document.querySelectorAll('.select-item:checked'),input=>input.dataset.magnet));prepareDownload(allResults.filter(item=>selected.has(item.magnet)));});
fnDownload.connected.then(()=>{$('#download-connection').textContent='飞牛桌面已连接';}).catch(()=>{$('#download-connection').textContent='从飞牛桌面打开可直接下载';});

const SEARCH_SOURCE_LABELS = Object.freeze({
  apibay: "APIBay API",
  "thepiratebay.org": "The Pirate Bay",
  "piratebay.live": "PirateBay Live 镜像",
  "pirateproxy.live": "PirateProxy 镜像",
  "tpb.party": "TPB Party 镜像",
  none: "未找到可用索引源",
});

function setActiveSource(source) {
  const label = SEARCH_SOURCE_LABELS[source] || "未知索引源";
  $("#active-source").textContent = label;
  return label;
}

function cleanTitle(value) {
  return value.trim()
    .replace(/\s+(?:Season\s+\d+|S\d+|第\s*\d+\s*季)(?:\s+Complete)?$/i, "")
    .replace(/\s+(?:\((?:19|20)\d{2}\)|（(?:19|20)\d{2}）)$/, "")
    .trim();
}

function containsChinese(value) {
  return /[\u3400-\u9fff]/.test(value);
}

function setStatus(message, isError = false) {
  statusBox.textContent = message;
  statusBox.classList.toggle("error", isError);
}

function safeDetailUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.href : "";
  } catch { return ""; }
}

function formatDate(timestamp) {
  if (!timestamp) return "未知日期";
  return new Date(timestamp * 1000).toLocaleDateString("zh-CN");
}

function readFilterNumber(selector) {
  const element = $(selector);
  const value = element?.value.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function readFilterInteger(selector, maximum) {
  const value = readFilterNumber(selector);
  return Number.isInteger(value) && value >= 1 && value <= maximum ? value : null;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function resultMetric(item, sort) {
  if (sort === "seeders_desc") {
    return item.seedersKnown === false ? null : finiteNumber(item.seeders);
  }
  if (sort === "season_episode_asc" || sort === "season_episode_desc") {
    const season = finiteNumber(item.season);
    const episode = finiteNumber(item.episode);
    return season === null ? null : season * 1000 + (episode ?? 0);
  }
  if (sort === "added_desc") {
    const added = finiteNumber(item.added);
    return added && added > 0 ? added : null;
  }
  if (sort === "size_desc" || sort === "size_asc") return finiteNumber(item.sizeBytes);
  return null;
}

function filterAndSortResults(results) {
  const resolution = $("#resolution-filter").value;
  const codec = $("#codec-filter").value;
  const season = readFilterInteger("#season-filter", 99);
  const episode = readFilterInteger("#episode-filter", 999);
  const sort = $("#result-sort").value;
  const ascending = sort.endsWith("_asc");

  return results.map((item, index) => ({item, index})).filter(({item}) => {
    if (resolution && (item.resolution || "other") !== resolution) return false;
    if (codec && (item.codec || "other") !== codec) return false;
    if (season !== null && finiteNumber(item.season) !== season) return false;
    if (episode !== null) {
      const episodeStart = finiteNumber(item.episode);
      const episodeEnd = finiteNumber(item.episodeEnd) ?? episodeStart;
      if (episodeStart === null || episode < episodeStart || episode > episodeEnd) return false;
    }
    return true;
  }).sort((left, right) => {
    const leftValue = resultMetric(left.item, sort);
    const rightValue = resultMetric(right.item, sort);
    if (leftValue === null && rightValue !== null) return 1;
    if (leftValue !== null && rightValue === null) return -1;
    if (leftValue !== null && rightValue !== null && leftValue !== rightValue) {
      return (leftValue - rightValue) * (ascending ? 1 : -1);
    }
    return left.index - right.index;
  }).map(({item}) => item);
}

function renderResults() {
  const visibleResults = filterAndSortResults(allResults);
  resultsBox.replaceChildren(...visibleResults.map(createResult));
  resultControls.classList.toggle("hidden", !activeSearch);
  toolbar.classList.toggle("hidden", allResults.length === 0);
  $("#result-count").textContent = `显示 ${visibleResults.length} / 共 ${allResults.length} 条结果`;
  selectAllButton.textContent = "全选";
  selectAllButton.disabled = visibleResults.length === 0;
  copySelectedButton.disabled = visibleResults.length === 0;
  searchFilteredButton.disabled = !activeSearch;
  if (!visibleResults.length && allResults.length) {
    const empty = document.createElement("p");
    empty.className = "empty-results";
    empty.textContent = "没有符合当前筛选条件的结果。";
    resultsBox.append(empty);
  }
}

function resetResultControls() {
  ["#season-filter", "#episode-filter"].forEach((selector) => { $(selector).value = ""; });
  ["#resolution-filter", "#codec-filter"].forEach((selector) => { $(selector).value = ""; });
  $("#result-sort").value = "seeders_desc";
}

function filterSearchQuery() {
  if (!activeSearch) return "";
  const terms = [activeSearch.baseTitle];
  const season = readFilterInteger("#season-filter", 99);
  const episode = readFilterInteger("#episode-filter", 999);
  if (season !== null || episode !== null) {
    let seasonEpisode = "";
    if (season !== null) seasonEpisode += `S${String(season).padStart(2, "0")}`;
    if (episode !== null) seasonEpisode += `E${String(episode).padStart(2, "0")}`;
    terms.push(seasonEpisode);
  }
  const resolution = $("#resolution-filter").value;
  if (resolution && resolution !== "other") terms.push(resolution);
  const codecTerms = {hevc: "x265", avc: "x264", av1: "AV1"};
  const codec = $("#codec-filter").value;
  if (codecTerms[codec]) terms.push(codecTerms[codec]);
  return terms.join(" ");
}

function createResult(item, index) {
  const article = document.createElement("article");
  article.className = "result";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "select-item";
  checkbox.dataset.magnet = item.magnet;
  checkbox.setAttribute("aria-label", `选择第 ${index + 1} 项`);

  const body = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = `${index + 1}. ${item.title}`;
  body.append(title);
  if (item.translation) {
    const translated = document.createElement("div");
    translated.className = "translated-title";
    translated.textContent = `中文说明：${item.translation}`;
    body.append(translated);
  }

  const tags = [];
  if (item.season) {
    let seasonEpisode = `S${String(item.season).padStart(2, "0")}`;
    if (item.episode) seasonEpisode += `E${String(item.episode).padStart(2, "0")}`;
    if (item.episodeEnd && item.episodeEnd !== item.episode) seasonEpisode += `-E${String(item.episodeEnd).padStart(2, "0")}`;
    tags.push(seasonEpisode);
  }
  if (item.releaseYear) tags.push(`发布名年份 ${item.releaseYear}`);
  if (item.resolution && item.resolution !== "other") tags.push(item.resolution);
  const sourceLabels = {remux: "Remux", webdl: "WEB-DL", webrip: "WEBRip", bluray: "BluRay", bdrip: "BDRip", hdtv: "HDTV", dvd: "DVD", cam: "CAM", ts: "TS"};
  const codecLabels = {hevc: "H.265 / x265", avc: "H.264 / x264", av1: "AV1"};
  if (sourceLabels[item.sourceFormat]) tags.push(sourceLabels[item.sourceFormat]);
  if (codecLabels[item.codec]) tags.push(codecLabels[item.codec]);
  if (tags.length) {
    const releaseMeta = document.createElement("div");
    releaseMeta.className = "release-meta";
    tags.forEach((text) => {
      const tag = document.createElement("span");
      tag.textContent = text;
      releaseMeta.append(tag);
    });
    body.append(releaseMeta);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  [`做种 ${item.seeders}`, `下载 ${item.leechers}`, `大小 ${item.size}`, formatDate(item.added), `上传者 ${item.uploader}`]
    .forEach((text) => {
      const span = document.createElement("span");
      span.textContent = text;
      meta.append(span);
    });

  const actions = document.createElement("div");
  actions.className = "actions";
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.textContent = "复制链接";
  copyButton.addEventListener("click", async () => {
    try {
      await copyText(item.magnet);
      copyButton.textContent = "已复制";
      setTimeout(() => { copyButton.textContent = "复制链接"; }, 1200);
    } catch { setStatus("浏览器拒绝了剪贴板操作。", true); }
  });
  const downloadButton = document.createElement("button");
  downloadButton.type = "button"; downloadButton.textContent = "下载到飞牛";
  downloadButton.addEventListener("click", () => prepareDownload([item]));
  copyButton.className = "secondary";
  actions.append(downloadButton, copyButton);
  const detailUrl = safeDetailUrl(item.detailUrl);
  if (detailUrl) {
    const link = document.createElement("a");
    link.href = detailUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "查看来源";
    actions.append(link);
  }
  body.append(meta, actions, createSubtitlePanel(item));
  article.append(checkbox, body);
  return article;
}

function showTitleOptions(options, originalQuery) {
  optionsBox.replaceChildren();
  optionsBox.classList.remove("hidden");
  const heading = document.createElement("h2");
  heading.textContent = `请选择“${originalQuery}”对应的英文片名`;
  optionsBox.append(heading);
  for (const option of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "title-option";
    const zh = document.createElement("strong");
    zh.textContent = option.chineseTitle;
    const en = document.createElement("span");
    en.textContent = option.englishTitle;
    const description = document.createElement("small");
    description.textContent = option.description || "影视作品";
    button.append(zh, en, description);
    button.addEventListener("click", () => {
      selectedMatch = option;
      queryInput.value = option.englishTitle;
      optionsBox.classList.add("hidden");
      searchResources(option.englishTitle, option.chineseTitle);
    });
    optionsBox.append(button);
  }
}

async function resolveChineseTitle(query) {
  const currentRequest = ++requestId;
  activeSearch = null;
  allResults = [];
  resetResultControls();
  resultsBox.replaceChildren();
  resultControls.classList.add("hidden");
  toolbar.classList.add("hidden");
  optionsBox.classList.add("hidden");
  setStatus(`正在识别“${query}”的英文片名…`);
  submitButton.disabled = true;
  try {
    const response = await fetch(`api/title-options?q=${encodeURIComponent(query)}`, { cache: "no-store" });
    const payload = await response.json();
    if (currentRequest !== requestId) return;
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (!payload.options.length) {
      setStatus(payload.warning || "没有识别到可靠的影视作品，请改用英文片名搜索。", true);
      return;
    }
    showTitleOptions(payload.options, query);
    setStatus(`找到 ${payload.options.length} 个可能的影视作品，请选择后搜索。`);
  } catch (error) {
    if (currentRequest === requestId) setStatus(error.message || "片名识别失败。", true);
  } finally {
    if (currentRequest === requestId) submitButton.disabled = false;
  }
}

async function searchResources(englishTitle, chineseTitle = "", options = {}) {
  const currentRequest = ++requestId;
  allResults = [];
  if (!options.preserveFilters) resetResultControls();
  activeSearch = {
    baseTitle: options.baseTitle || englishTitle,
    chineseTitle,
  };
  resultsBox.replaceChildren();
  resultControls.classList.add("hidden");
  toolbar.classList.add("hidden");
  setStatus(options.filteredSearch
    ? `正在按筛选条件搜索“${englishTitle}”…`
    : `正在使用英文片名“${englishTitle}”搜索…`);
  $("#active-source").textContent = "查询中…";
  submitButton.disabled = true;
  try {
    const params = new URLSearchParams({ q: englishTitle });
    if (chineseTitle) params.set("zh", chineseTitle);
    if (options.baseTitle && options.baseTitle !== englishTitle) params.set("base", options.baseTitle);
    const response = await fetch(`api/search?${params}`, { cache: "no-store" });
    const payload = await response.json();
    if (currentRequest !== requestId) return;
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (!Array.isArray(payload.results)) throw new Error("搜索服务返回了无法识别的数据");
    const sourceLabel = setActiveSource(payload.source);
    allResults = payload.results;
    renderResults();
    const translatedCount = allResults.filter((item) => item.chineseTitle).length;
    const matchText = translatedCount ? `；已识别 ${translatedCount} 条结果的中文片名` : "";
    setStatus(allResults.length
      ? `${options.filteredSearch ? "筛选后搜索完成" : "搜索完成"}，共 ${allResults.length} 条结果${matchText}；来源：${sourceLabel}。`
      : `没有找到结果，可以调整筛选条件后重试。来源：${sourceLabel}。`);
  } catch (error) {
    if (currentRequest === requestId) {
      resultControls.classList.toggle("hidden", !activeSearch);
      $("#active-source").textContent = "连接失败";
      setStatus(error.message || "搜索失败。", true);
    }
  } finally {
    if (currentRequest === requestId) submitButton.disabled = false;
  }
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.append(helper);
  helper.select();
  const copied = document.execCommand("copy");
  helper.remove();
  if (!copied) throw new Error("copy failed");
}

async function copyMagnets(items) {
  if (!items.length) return setStatus("请至少选择一项。", true);
  try {
    await copyText(items.join("\n"));
    setStatus(`已复制 ${items.length} 条链接。`);
  } catch { setStatus("浏览器拒绝了剪贴板操作，请检查剪贴板权限。", true); }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = cleanTitle(queryInput.value);
  queryInput.value = query;
  if (!query) return;
  if (containsChinese(query)) resolveChineseTitle(query);
  else searchResources(query, selectedMatch?.englishTitle === query ? selectedMatch.chineseTitle : "");
});

queryInput.addEventListener("input", () => {
  if (selectedMatch && queryInput.value !== selectedMatch.englishTitle) selectedMatch = null;
  if (activeSearch && queryInput.value.trim() !== activeSearch.baseTitle) {
    activeSearch = null;
    searchFilteredButton.disabled = true;
  }
});

document.querySelectorAll("#result-controls input, #result-controls select").forEach((control) => {
  control.addEventListener("input", renderResults);
  control.addEventListener("change", renderResults);
});

$("#reset-result-controls").addEventListener("click", () => {
  resetResultControls();
  renderResults();
});

searchFilteredButton.addEventListener("click", () => {
  if (!activeSearch) return setStatus("请先完成一次片名搜索。", true);
  const filteredQuery = filterSearchQuery();
  if (filteredQuery === activeSearch.baseTitle) {
    if ($("#resolution-filter").value === "other" || $("#codec-filter").value === "other") {
      return setStatus("“其他/未知”和排序只作用于当前结果，不能作为新的搜索关键词。", true);
    }
    return setStatus("请先选择清晰度、编码、季或集，再执行筛选后搜索。", true);
  }
  if (filteredQuery.length > 200) return setStatus("片名与筛选条件合计不能超过 200 个字符。", true);
  const {baseTitle, chineseTitle} = activeSearch;
  searchResources(filteredQuery, chineseTitle, {
    preserveFilters: true,
    filteredSearch: true,
    baseTitle,
  });
});

selectAllButton.addEventListener("click", () => {
  const boxes = [...document.querySelectorAll(".select-item")];
  const shouldCheck = boxes.some((box) => !box.checked);
  boxes.forEach((box) => { box.checked = shouldCheck; });
  selectAllButton.textContent = shouldCheck ? "取消全选" : "全选";
});

copySelectedButton.addEventListener("click", () => {
  const magnets = [...document.querySelectorAll(".select-item:checked")].map((box) => box.dataset.magnet);
  copyMagnets(magnets);
});

function loadInitialSearch() {
  const value = new URLSearchParams(location.search).get("q")?.trim() || "";
  if (!value || value.length > 200) return;
  const query = cleanTitle(value);
  queryInput.value = query;
  if (containsChinese(query)) resolveChineseTitle(query);
  else searchResources(query);
}

loadInitialSearch();

$('#magnet-form').addEventListener('submit', event => {
  event.preventDefault();
  const magnet=$('#magnet-input').value.trim();
  try {const url=new URL(magnet); if(url.protocol!=='magnet:' || !url.searchParams.getAll('xt').some(x=>/^urn:btih:(?:[a-f0-9]{40}|[a-z2-7]{32})$/i.test(x)))throw new Error(); prepareDownload([{magnet,title:url.searchParams.get('dn')||'手动添加的磁力链接'}]);}
  catch {setStatus('请输入完整、有效的磁力链接。',true);}
});
