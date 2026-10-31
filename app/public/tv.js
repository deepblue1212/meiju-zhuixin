const $ = (selector) => document.querySelector(selector);
const discoverList = $("#discover-list");
const discoverStatus = $("#discover-status");
const refreshButton = $("#refresh-discover");
let requestId = 0;

function createTvItem(item) {
  const article = document.createElement("article");
  article.className = "discover-item";

  const title = document.createElement("a");
  title.className = "discover-title";
  title.textContent = item.title;
  title.href = searchUrl(item.title);
  title.setAttribute("aria-label", `搜索《${item.title}》的资源`);

  const rating = document.createElement("div");
  rating.className = "discover-rating";
  rating.textContent = `豆瓣评分：${item.rate}`;

  article.append(title, rating);
  return article;
}

function searchUrl(title) {
  const target = new URL("index.html", location.href);
  target.searchParams.set("q", title);
  return target.href;
}

async function loadLatestTv(force = false) {
  const currentRequest = ++requestId;
  refreshButton.disabled = true;
  discoverStatus.textContent = "正在读取豆瓣最新美剧…";
  discoverStatus.classList.remove("error");
  try {
    const endpoint = force ? "api/discover?category=us_tv&refresh=1" : "api/discover?category=us_tv";
    const response = await fetch(endpoint, { cache: "no-store" });
    const payload = await response.json();
    if (currentRequest !== requestId) return;
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    discoverList.replaceChildren(...payload.items.map(createTvItem));
    const partialNotice = payload.partial ? " 后续数据读取失败，可点击刷新重试。" : "";
    discoverStatus.textContent = payload.items.length ? `共 ${payload.items.length} 部。${partialNotice}` : "豆瓣暂未返回美剧数据。";
  } catch (error) {
    if (currentRequest !== requestId) return;
    discoverList.replaceChildren();
    discoverStatus.textContent = error.message || "豆瓣最新美剧读取失败。";
    discoverStatus.classList.add("error");
  } finally {
    if (currentRequest === requestId) refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", () => loadLatestTv(true));
loadLatestTv();
