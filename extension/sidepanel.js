/**
 * 侧边栏交互逻辑。
 * 流程：读当前页 → 注入 content.js 提取岗位 → POST 本地后端 → 渲染结果。
 */

const API_URL = "http://127.0.0.1:8000/api/analyze";
const COPY_RESET_MS = 1600;

const SOURCE_LABEL = {
  selection: "选中文本",
  page: "页面抓取",
  "selection-short": "选中过短",
  none: "无内容",
};

const els = {
  button: document.getElementById("analyzeBtn"),
  status: document.getElementById("status"),
  result: document.getElementById("result"),
  jobTitle: document.getElementById("jobTitle"),
  sourceBadge: document.getElementById("sourceBadge"),
  ring: document.getElementById("ring"),
  score: document.getElementById("score"),
  scoreLabel: document.getElementById("scoreLabel"),
  pros: document.getElementById("pros"),
  gaps: document.getElementById("gaps"),
  greetings: document.getElementById("greetings"),
  meta: document.getElementById("meta"),
};

class ApiError extends Error {
  constructor(status, detail) {
    super(`HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

const toMessage = (detail) =>
  Array.isArray(detail) ? detail.map((item) => item.msg || "").join("；") : detail || "";

function setStatus(kind, text) {
  if (!text) {
    els.status.hidden = true;
    return;
  }
  els.status.hidden = false;
  els.status.className = `status ${kind}`;
  els.status.textContent = text;
}

function setLoading(loading) {
  els.button.disabled = loading;
  els.button.classList.toggle("loading", loading);
  els.button.textContent = loading ? "分析中" : "分析当前岗位";
}

function scoreColor(score) {
  if (score >= 80) return "#15a05a";
  if (score >= 60) return "#d97706";
  return "#dc2626";
}

function scoreLabel(score) {
  if (score >= 85) return "高度匹配";
  if (score >= 70) return "比较匹配";
  if (score >= 55) return "基本匹配";
  if (score >= 40) return "存在跨度";
  return "差距较大";
}

async function getActiveTab() {
  // 侧边栏自身获得焦点时 currentWindow 未必是目标窗口，lastFocusedWindow 更稳
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !tab.id) throw new Error("找不到当前标签页。");

  // 浏览器内部页面无法注入脚本，提前拦下并说清原因（这也是申请 tabs 权限的用处）
  if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(tab.url || "")) {
    throw new Error(`当前是浏览器内部页面（${tab.url}），请在 Boss 直聘岗位详情页使用。`);
  }

  return tab;
}

/** 注入 content.js 并取回提取结果；失败时给出可操作的提示。 */
async function extractJob() {
  const tab = await getActiveTab();
  let data = null;

  try {
    const [first] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"],
    });
    data = first && first.result;

    if (!data) {
      const [second] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (window.__bossCopilotExtract ? window.__bossCopilotExtract() : null),
      });
      data = second && second.result;
    }
  } catch (error) {
    // 把 Chrome 的原始报错带上，否则永远只能看到一句笼统的"无法读取"
    const reason = error && error.message ? `（${error.message}）` : "";
    throw new Error(
      `无法读取当前页面${reason}。请确认当前标签页是 Boss 直聘岗位页（https://*.zhipin.com），` +
        "并在该页面上重新点击工具栏的 BossCopilot 图标后再试。"
    );
  }

  if (!data || !data.jd_text) {
    throw new Error("没有抓到岗位描述。请在页面上用鼠标选中 JD 正文，再点一次「分析当前岗位」。");
  }

  return data;
}

async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 侧边栏在个别环境下拿不到 clipboard 权限，退回 execCommand
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    helper.remove();
    return ok;
  }
}

function renderList(container, items, kind) {
  container.replaceChildren();
  items.forEach((text) => {
    const li = document.createElement("li");
    const mark = document.createElement("span");
    mark.className = `mark ${kind}`;
    mark.textContent = kind === "ok" ? "✓" : "!";
    const body = document.createElement("span");
    body.textContent = text;
    li.append(mark, body);
    container.appendChild(li);
  });
}

function renderGreetings(items) {
  els.greetings.replaceChildren();
  items.forEach((text) => {
    const card = document.createElement("article");
    card.className = "greet";

    const paragraph = document.createElement("p");
    paragraph.textContent = text;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "复制";
    button.addEventListener("click", async () => {
      const ok = await writeClipboard(text);
      button.textContent = ok ? "已复制" : "复制失败";
      button.classList.toggle("copied", ok);
      clearTimeout(button._resetTimer);
      button._resetTimer = setTimeout(() => {
        button.textContent = "复制";
        button.classList.remove("copied");
      }, COPY_RESET_MS);
    });

    card.append(paragraph, button);
    els.greetings.appendChild(card);
  });
}

function isValidPayload(data) {
  return (
    data &&
    typeof data.match_score === "number" &&
    Array.isArray(data.pros) &&
    Array.isArray(data.gaps) &&
    Array.isArray(data.greetings)
  );
}

function render(job, data, elapsedMs) {
  els.jobTitle.textContent = job.job_title;
  els.sourceBadge.textContent = SOURCE_LABEL[job.source] || "页面";

  const score = Math.max(0, Math.min(100, Math.round(data.match_score)));
  els.score.textContent = String(score);
  els.ring.style.setProperty("--p", String(score));
  els.ring.style.setProperty("--ring-color", scoreColor(score));
  els.scoreLabel.textContent = scoreLabel(score);

  renderList(els.pros, data.pros, "ok");
  renderList(els.gaps, data.gaps, "warn");
  renderGreetings(data.greetings);

  els.meta.textContent = `${data.pros.length} 条优势 · ${data.gaps.length} 条缺口 · 用时 ${(elapsedMs / 1000).toFixed(1)}s`;
  els.result.hidden = false;
  els.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 503) return `后端缺少配置：${toMessage(error.detail)}`;
    if (error.status === 502) return `模型调用失败：${toMessage(error.detail)}`;
    if (error.status === 422) return "JD 文本太短或为空。请选中完整的职位描述段落（至少 10 个字）后重试。";
    return `请求失败（HTTP ${error.status}）：${toMessage(error.detail)}`;
  }
  if (error instanceof TypeError) {
    return "连接不上后端。请确认已在 backend 目录运行：uvicorn backend.main:app --reload --port 8000";
  }
  return error.message || String(error);
}

async function analyze() {
  if (els.button.disabled) return;

  setLoading(true);
  els.result.hidden = true;
  setStatus("info", "正在读取当前页面…");

  const startedAt = performance.now();

  try {
    const job = await extractJob();
    setStatus(
      "info",
      `已获取「${job.job_title}」（${SOURCE_LABEL[job.source] || "页面"}），DeepSeek 分析中…`
    );

    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_title: job.job_title, jd_text: job.jd_text }),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, payload.detail);
    if (!isValidPayload(payload)) throw new Error("后端返回结构异常：缺少 match_score / pros / gaps / greetings。");

    render(job, payload, Math.round(performance.now() - startedAt));
    setStatus("", "");
  } catch (error) {
    setStatus("error", describeError(error));
  } finally {
    setLoading(false);
  }
}

els.button.addEventListener("click", analyze);
