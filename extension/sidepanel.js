/**
 * 侧边栏交互逻辑。
 * 流程：读当前页 → 注入 content.js 提取岗位 → POST 本地后端 → 渲染结果。
 */

const API_URL = "http://127.0.0.1:8000/api/analyze";
const INTERVIEW_URL = "http://127.0.0.1:8000/api/interview";
const RESUME_URL = "http://127.0.0.1:8000/api/resume";
const UPLOAD_URL = "http://127.0.0.1:8000/api/resume/upload";
const TEST_CONNECTION_URL = "http://127.0.0.1:8000/api/test-connection";
const SUGGEST_REPLY_URL = "http://127.0.0.1:8000/api/suggest-reply";
const API_STORAGE_KEY = "bossCopilotApiConfig";
const COPY_RESET_MS = 1600;

/** 服务商预设。选中后自动填充 Base URL 与 Model，用户仍可手改。 */
const PROVIDERS = {
  zhipu: {
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-4-flash",
    note: "glm-4-flash 有免费额度，注册智谱开放平台即可拿 Key",
  },
  siliconflow: {
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "Qwen/Qwen2.5-7B-Instruct",
    note: "硅基流动提供免费开源模型，注册即可用",
  },
  deepseek: {
    baseUrl: "https://api.deepseek.com",
    model: "deepseek-chat",
    note: "默认后端配置，需要账户余额",
  },
  volcengine: {
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    model: "",
    note: "Model 必须填接入点 ID（ep- 开头），不是模型名",
  },
  custom: {
    baseUrl: "",
    model: "",
    note: "填任何 OpenAI 兼容服务的 Base URL 与模型名",
  },
};

let apiConfig = { provider: "deepseek", apiKey: "", baseUrl: "", model: "" };

const SOURCE_LABEL = {
  selection: "选中文本",
  "page-detail": "详情区",
  page: "页面抓取",
  "list-no-detail": "列表页",
  "selection-short": "选中过短",
  none: "无内容",
};

const els = {
  button: document.getElementById("analyzeBtn"),
  interviewBtn: document.getElementById("interviewBtn"),
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
  interview: document.getElementById("interview"),
  interviewMeta: document.getElementById("interviewMeta"),
  interviewBody: document.getElementById("interviewBody"),
  resumePanel: document.getElementById("resumePanel"),
  resumeBadge: document.getElementById("resumeBadge"),
  resumeText: document.getElementById("resumeText"),
  resumeFile: document.getElementById("resumeFile"),
  resumeHint: document.getElementById("resumeHint"),
  uploadBtn: document.getElementById("uploadBtn"),
  saveResumeBtn: document.getElementById("saveResumeBtn"),
  apiPanel: document.getElementById("apiPanel"),
  apiBadge: document.getElementById("apiBadge"),
  providerSelect: document.getElementById("providerSelect"),
  apiKeyInput: document.getElementById("apiKeyInput"),
  baseUrlInput: document.getElementById("baseUrlInput"),
  modelInput: document.getElementById("modelInput"),
  testApiBtn: document.getElementById("testApiBtn"),
  saveApiBtn: document.getElementById("saveApiBtn"),
  apiHint: document.getElementById("apiHint"),
  tabJobBtn: document.getElementById("tabJobBtn"),
  tabChatBtn: document.getElementById("tabChatBtn"),
  jobPanel: document.getElementById("jobPanel"),
  chatPanel: document.getElementById("chatPanel"),
  readChatBtn: document.getElementById("readChatBtn"),
  chatStatus: document.getElementById("chatStatus"),
  chatContext: document.getElementById("chatContext"),
  chatJobTitle: document.getElementById("chatJobTitle"),
  chatSalary: document.getElementById("chatSalary"),
  chatCompany: document.getElementById("chatCompany"),
  chatCount: document.getElementById("chatCount"),
  manualPanel: document.getElementById("manualPanel"),
  manualHrText: document.getElementById("manualHrText"),
  manualSubmitBtn: document.getElementById("manualSubmitBtn"),
  replyResult: document.getElementById("replyResult"),
  intentBadge: document.getElementById("intentBadge"),
  strategyText: document.getElementById("strategyText"),
  replyCards: document.getElementById("replyCards"),
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

function setBusy(mode) {
  const busy = Boolean(mode);
  els.button.disabled = busy;
  els.interviewBtn.disabled = busy;
  els.button.classList.toggle("loading", mode === "analyze");
  els.interviewBtn.classList.toggle("loading", mode === "interview");
  els.button.textContent = mode === "analyze" ? "分析中" : "分析当前岗位";
  els.interviewBtn.textContent = mode === "interview" ? "生成中" : "生成面试题";
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
    const message =
      data && data.source === "list-no-detail"
        ? "这是岗位列表页，没找到右侧的职位详情。请先点开一个岗位让它加载出来，或用鼠标选中 JD 正文再试。"
        : "没有抓到岗位描述。请在页面上用鼠标选中 JD 正文，再点一次「分析当前岗位」。";
    throw new Error(message);
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
    if (error.status === 400) return `文件或内容有问题：${toMessage(error.detail)}`;
    return `请求失败（HTTP ${error.status}）：${toMessage(error.detail)}`;
  }
  if (error instanceof TypeError) {
    return "连接不上后端。请在 backend 目录运行：.venv\\Scripts\\python.exe -m uvicorn main:app --port 8000";
  }
  return error.message || String(error);
}

async function analyze() {
  if (els.button.disabled) return;

  setBusy("analyze");
  els.result.hidden = true;
  setStatus("info", "正在读取当前页面…");

  const startedAt = performance.now();

  try {
    const job = await extractJob();
    setStatus(
      "info",
      `已获取「${job.job_title}」（${SOURCE_LABEL[job.source] || "页面"}），模型分析中…`
    );

    const response = await fetch(API_URL, {
      method: "POST",
      headers: apiHeadersFrom(apiConfig, { "Content-Type": "application/json" }),
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
    setBusy(null);
  }
}

const INTERVIEW_GROUPS = [
  ["project_questions", "简历项目深挖"],
  ["gap_questions", "岗位短板追问"],
  ["scenario_questions", "业务场景设计"],
];

function isValidInterview(data) {
  return data && INTERVIEW_GROUPS.every(([key]) => Array.isArray(data[key]));
}

function renderInterview(job, data) {
  const total = INTERVIEW_GROUPS.reduce((sum, [key]) => sum + data[key].length, 0);
  els.interviewMeta.textContent = `${total} 题`;
  els.interviewBody.replaceChildren();

  INTERVIEW_GROUPS.forEach(([key, label]) => {
    const items = data[key];
    if (!items.length) return;

    const title = document.createElement("h3");
    title.className = "group-title";
    title.textContent = `${label} · ${items.length} 题`;
    els.interviewBody.appendChild(title);

    items.forEach((item, index) => {
      const card = document.createElement("details");
      card.className = "q";

      const summary = document.createElement("summary");
      const order = document.createElement("span");
      order.className = "qno";
      order.textContent = `${index + 1}.`;
      summary.append(order, document.createTextNode(item.question));

      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = item.answer_hint;

      card.append(summary, hint);
      els.interviewBody.appendChild(card);
    });
  });

  els.interview.hidden = false;
  els.interview.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function generateInterview() {
  if (els.interviewBtn.disabled) return;

  setBusy("interview");
  els.interview.hidden = true;
  setStatus("info", "正在读取当前页面…");

  try {
    const job = await extractJob();
    setStatus("info", `已获取「${job.job_title}」，正在生成 8 道面试题（约 10 秒）…`);

    const response = await fetch(INTERVIEW_URL, {
      method: "POST",
      headers: apiHeadersFrom(apiConfig, { "Content-Type": "application/json" }),
      body: JSON.stringify({ job_title: job.job_title, jd_text: job.jd_text }),
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, payload.detail);
    if (!isValidInterview(payload)) {
      throw new Error("后端返回结构异常：缺少 project_questions / gap_questions / scenario_questions。");
    }

    renderInterview(job, payload);
    setStatus("", "");
  } catch (error) {
    setStatus("error", describeError(error));
  } finally {
    setBusy(null);
  }
}

/* ------------------------------------------------------------------ *
 * 帮我回复：聊天页读屏 → 意图 + 3 条回复 → 复制 / 填入输入框
 * 只读当前页，只在用户点击时注入 chat.js，绝不自动发送。
 * ------------------------------------------------------------------ */

let lastChatJob = null;

const CHAT_FAIL_REASON = {
  "not-chat-page": "当前不是聊天页。请打开 Boss 直聘的「沟通」页，再点这个按钮。",
  "no-record": "没找到聊天窗口，可能页面还没加载完。等两秒重试，或用下面的手动粘贴。",
  "no-messages": "聊天区没读到消息，可能是虚拟滚动或页面改版。用下面的手动粘贴兜底。",
  "read-error": "读取脚本出错了，请刷新页面重试，或用下面的手动粘贴。",
};

function setChatStatus(kind, text) {
  if (!text) {
    els.chatStatus.hidden = true;
    return;
  }
  els.chatStatus.hidden = false;
  els.chatStatus.className = `status ${kind}`;
  els.chatStatus.textContent = text;
}

function setChatBusy(busy) {
  els.readChatBtn.disabled = busy;
  els.manualSubmitBtn.disabled = busy;
  els.readChatBtn.classList.toggle("loading", busy);
  els.readChatBtn.textContent = busy ? "处理中" : "读取当前对话并推荐回复";
}

function switchTab(name) {
  const isJob = name === "job";
  els.jobPanel.hidden = !isJob;
  els.chatPanel.hidden = isJob;
  els.tabJobBtn.classList.toggle("active", isJob);
  els.tabChatBtn.classList.toggle("active", !isJob);
  els.tabJobBtn.setAttribute("aria-selected", String(isJob));
  els.tabChatBtn.setAttribute("aria-selected", String(!isJob));
}

function renderChatContext(job, count) {
  const title = (job && job.title) || "";
  const company = (job && job.company) || "";
  const salary = (job && job.salary) || "";

  if (!title && !company && !salary && !count) {
    els.chatContext.hidden = true;
    return;
  }

  els.chatJobTitle.textContent = title || "当前对话";
  els.chatSalary.textContent = salary;
  els.chatSalary.hidden = !salary;
  els.chatCompany.textContent = company;
  els.chatCompany.hidden = !company;
  els.chatCount.textContent = count ? `读取到最近 ${count} 条消息` : "未读取到聊天记录";
  els.chatContext.hidden = false;
}

/** 注入 chat.js 并取回读屏结果。任何注入失败都转成可读文案。 */
async function readChatPage() {
  const tab = await getActiveTab();
  if (!/zhipin\.com/.test(tab.url || "")) {
    throw new Error("请在 Boss 直聘聊天页（/web/geek/chat）使用这个功能。");
  }

  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["chat.js"] });
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (window.__bossCopilotChat ? window.__bossCopilotChat.read() : null),
    });
    return result && result.result;
  } catch (error) {
    const reason = error && error.message ? `（${error.message}）` : "";
    throw new Error(`无法读取聊天页${reason}。请确认标签页是 Boss 直聘聊天页，或直接用下面的手动粘贴。`);
  }
}

function isValidReplyPayload(data) {
  return (
    data &&
    typeof data.intent === "string" &&
    typeof data.strategy === "string" &&
    Array.isArray(data.replies) &&
    data.replies.length === 3 &&
    data.replies.every(
      (item) => item && typeof item.style === "string" && typeof item.content === "string"
    )
  );
}

async function fillChatInput(text, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "填入中…";

  try {
    const tab = await getActiveTab();
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["chat.js"] });
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (value) =>
        window.__bossCopilotChat
          ? window.__bossCopilotChat.fill(value)
          : { ok: false, reason: "no-bridge" },
      args: [text],
    });

    const payload = result && result.result;
    if (!payload || !payload.ok) {
      const message =
        payload && payload.reason === "input-not-found"
          ? "没找到聊天输入框，请点「复制」后手动粘贴。"
          : "填入失败，请点「复制」后手动粘贴。";
      throw new Error(message);
    }

    button.textContent = "已填入";
    setChatStatus(
      "info",
      "已填入聊天输入框。请先检查内容，再自己点发送——工具不会自动发送。"
    );
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, COPY_RESET_MS);
  } catch (error) {
    button.textContent = original;
    button.disabled = false;
    setChatStatus("error", error.message || String(error));
  }
}

function renderReplies(data) {
  els.intentBadge.textContent = data.intent;
  els.intentBadge.className = "badge ok";
  els.strategyText.textContent = data.strategy || "（模型没有给出策略，按回复内容判断）";
  els.replyCards.replaceChildren();

  data.replies.forEach((item) => {
    const card = document.createElement("article");
    card.className = "reply";

    const head = document.createElement("div");
    head.className = "reply-head";
    const tag = document.createElement("span");
    tag.className = "style-tag";
    tag.textContent = item.style;
    head.appendChild(tag);

    const text = document.createElement("p");
    text.className = "reply-text";
    text.textContent = item.content;

    const actions = document.createElement("div");
    actions.className = "reply-actions";

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.textContent = "复制";
    copyBtn.addEventListener("click", async () => {
      const ok = await writeClipboard(item.content);
      copyBtn.textContent = ok ? "已复制" : "复制失败";
      clearTimeout(copyBtn._resetTimer);
      copyBtn._resetTimer = setTimeout(() => {
        copyBtn.textContent = "复制";
      }, COPY_RESET_MS);
    });

    const fillBtn = document.createElement("button");
    fillBtn.type = "button";
    fillBtn.className = "primary";
    fillBtn.textContent = "填入输入框";
    fillBtn.addEventListener("click", () => fillChatInput(item.content, fillBtn));

    actions.append(copyBtn, fillBtn);
    card.append(head, text, actions);
    els.replyCards.appendChild(card);
  });

  els.replyResult.hidden = false;
  els.replyResult.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function requestReplies(jobInfo, messages) {
  const startedAt = performance.now();
  const response = await fetch(SUGGEST_REPLY_URL, {
    method: "POST",
    headers: apiHeadersFrom(apiConfig, { "Content-Type": "application/json" }),
    body: JSON.stringify({ job_info: jobInfo, chat_history: messages }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, payload.detail);
  if (!isValidReplyPayload(payload)) {
    throw new Error("后端返回结构异常：缺少 intent / strategy / replies（3 条）。");
  }

  renderReplies(payload);
  const elapsed = (performance.now() - startedAt) / 1000;
  setChatStatus("info", `意图：${payload.intent} · 3 条回复 · 用时 ${elapsed.toFixed(1)}s`);
}

function describeChatError(error) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      "后端没有 /api/suggest-reply 接口，说明还在跑旧进程。" +
      "请在启动 uvicorn 的终端按 Ctrl+C，然后重新执行：" +
      ".venv\\Scripts\\python.exe -m uvicorn main:app --port 8000 --reload"
    );
  }
  if (error instanceof ApiError && error.status === 422) {
    return "聊天记录为空或格式不对，请重试或用下面的手动粘贴。";
  }
  return describeError(error);
}

async function readAndSuggest() {
  if (els.readChatBtn.disabled) return;

  setChatBusy(true);
  els.replyResult.hidden = true;
  setChatStatus("info", "正在读取当前聊天页…");

  try {
    const data = await readChatPage();

    if (data && data.job_info) {
      lastChatJob = data.job_info;
      renderChatContext(data.job_info, data.count || 0);
    }

    if (!data || !data.ok) {
      els.manualPanel.open = true;
      setChatStatus(
        "warn",
        CHAT_FAIL_REASON[data && data.reason] || "没读到聊天记录，请用下面的手动粘贴。"
      );
      return;
    }

    setChatStatus("info", `已读取 ${data.count} 条消息，正在判断意图并生成回复…`);
    await requestReplies(data.job_info || {}, data.messages || []);
    els.manualPanel.open = false;
  } catch (error) {
    els.manualPanel.open = true;
    setChatStatus("error", describeChatError(error));
  } finally {
    setChatBusy(false);
  }
}

async function suggestFromManual() {
  if (els.manualSubmitBtn.disabled) return;

  const text = els.manualHrText.value.trim();
  if (!text) {
    setChatStatus("warn", "先粘贴 HR 的最后一条消息，再生成。");
    els.manualHrText.focus();
    return;
  }

  setChatBusy(true);
  els.replyResult.hidden = true;
  setChatStatus("info", "正在按手动粘贴的内容生成回复…");

  try {
    if (lastChatJob) {
      renderChatContext(lastChatJob, 1);
    } else {
      els.chatContext.hidden = true;
    }
    await requestReplies(lastChatJob || { company: "", title: "", salary: "" }, [
      { role: "hr", content: text },
    ]);
  } catch (error) {
    setChatStatus("error", describeChatError(error));
  } finally {
    setChatBusy(false);
  }
}

function setResumeBadge(kind, text) {
  els.resumeBadge.className = `badge ${kind}`;
  els.resumeBadge.textContent = text;
}

function applyResumeState(data, hint) {
  els.resumeText.value = data.content || "";
  setResumeBadge(data.exists ? "ok" : "warn", data.exists ? "已配置" : "未配置");
  els.resumeHint.textContent = hint || (data.exists ? `${data.chars} 字` : "上传或粘贴后保存");
}

/** 打开侧边栏时拉取一次，回填文本框并刷新状态标签。 */
async function loadResume() {
  try {
    const response = await fetch(RESUME_URL);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    applyResumeState(await response.json());
  } catch {
    setResumeBadge("warn", "未配置");
    els.resumeHint.textContent = "后端未连接";
  }
}

async function saveResume() {
  const content = els.resumeText.value.trim();
  if (!content) {
    setStatus("warn", "简历内容为空：先粘贴文本或上传文件。");
    return;
  }

  els.saveResumeBtn.disabled = true;
  els.saveResumeBtn.textContent = "保存中…";

  try {
    const response = await fetch(RESUME_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, data.detail);

    applyResumeState(data, `${data.chars} 字`);
    els.resumePanel.open = false;

    const successText = "简历已保存，可以直接分析岗位了。";
    setStatus("info", successText);
    setTimeout(() => {
      if (els.status.textContent === successText) setStatus("", "");
    }, 3000);
  } catch (error) {
    setStatus("error", describeError(error));
  } finally {
    els.saveResumeBtn.disabled = false;
    els.saveResumeBtn.textContent = "保存简历";
  }
}

async function uploadResume(file) {
  const form = new FormData();
  form.append("file", file);

  els.uploadBtn.disabled = true;
  els.uploadBtn.textContent = "解析中…";
  els.resumeHint.textContent = `正在解析 ${file.name}`;

  try {
    const response = await fetch(UPLOAD_URL, { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, data.detail);

    applyResumeState(data, `${file.name} · ${data.chars} 字`);
    setStatus("info", "简历已解析并写入，可在文本框里微调后再次保存。");
  } catch (error) {
    setStatus("error", describeError(error));
    els.resumeHint.textContent = "上传失败";
  } finally {
    els.uploadBtn.disabled = false;
    els.uploadBtn.textContent = "上传 PDF / MD / TXT";
    els.resumeFile.value = ""; // 允许再次选择同一个文件
  }
}

/** chrome.storage 在 file:// 预览里不存在，做一层兜底，避免整页脚本挂掉。 */
const apiStorage = (() => {
  try {
    return chrome.storage && chrome.storage.local ? chrome.storage.local : null;
  } catch {
    return null;
  }
})();

function updateApiBadge() {
  const configured = Boolean(apiConfig.apiKey);
  els.apiBadge.className = `badge ${configured ? "ok" : "warn"}`;
  els.apiBadge.textContent = configured ? "已配置" : "后端默认";
}

function fillProviderPreset(providerKey) {
  const preset = PROVIDERS[providerKey] || PROVIDERS.custom;
  els.baseUrlInput.value = preset.baseUrl;
  els.modelInput.value = preset.model;
  els.apiHint.textContent = preset.note || "仅保存在本机浏览器，不会上传";
}

function renderApiForm() {
  const providerKey = PROVIDERS[apiConfig.provider] ? apiConfig.provider : "deepseek";
  els.providerSelect.value = providerKey;
  els.apiKeyInput.value = apiConfig.apiKey || "";
  els.baseUrlInput.value = apiConfig.baseUrl || PROVIDERS[providerKey].baseUrl;
  els.modelInput.value = apiConfig.model || PROVIDERS[providerKey].model;
  els.apiHint.textContent = PROVIDERS[providerKey].note || "仅保存在本机浏览器，不会上传";
  updateApiBadge();
}

async function loadApiConfig() {
  if (!apiStorage) return;
  try {
    const data = await apiStorage.get(API_STORAGE_KEY);
    const saved = data && data[API_STORAGE_KEY];
    if (saved && typeof saved === "object") apiConfig = { ...apiConfig, ...saved };
  } catch {
    /* 读不出来就用默认值，不影响其它功能 */
  }
}

/** 把配置拼成后端认识的三个请求头；未填的字段不发，让后端回落 .env。 */
function apiHeadersFrom(source, base = {}) {
  const headers = { ...base };
  if (source.apiKey) headers["X-API-Key"] = source.apiKey;
  if (source.baseUrl) headers["X-Base-URL"] = source.baseUrl;
  if (source.model) headers["X-Model-Name"] = source.model;
  return headers;
}

async function saveApiConfig() {
  apiConfig = {
    provider: els.providerSelect.value,
    apiKey: els.apiKeyInput.value.trim(),
    baseUrl: els.baseUrlInput.value.trim(),
    model: els.modelInput.value.trim(),
  };

  if (apiStorage) {
    try {
      await apiStorage.set({ [API_STORAGE_KEY]: apiConfig });
    } catch {
      setStatus("error", "写入浏览器存储失败，配置只在本次会话有效。");
    }
  }

  updateApiBadge();
  els.apiPanel.open = false;

  const message = apiConfig.apiKey
    ? "API 配置已保存到本机浏览器。"
    : "已清空自定义 Key，将使用后端 .env 的配置。";
  setStatus("info", message);
  setTimeout(() => {
    if (els.status.textContent === message) setStatus("", "");
  }, 3000);
}

async function testApiConnection() {
  // 用表单里的当前值测，而不是上次保存的值——避免"改了没保存就测"的误导
  const draft = {
    apiKey: els.apiKeyInput.value.trim(),
    baseUrl: els.baseUrlInput.value.trim(),
    model: els.modelInput.value.trim(),
  };

  els.testApiBtn.disabled = true;
  els.testApiBtn.textContent = "测试中…";
  els.apiHint.textContent = "正在发送 hi 测试…";

  try {
    const response = await fetch(TEST_CONNECTION_URL, {
      method: "POST",
      headers: apiHeadersFrom(draft),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(response.status, data.detail);

    els.apiHint.textContent = data.ok
      ? `✓ 连接成功 · ${data.model} · ${data.latency_ms}ms`
      : `✗ ${data.error || "连接失败"}`;
  } catch (error) {
    els.apiHint.textContent = `✗ ${describeError(error)}`;
  } finally {
    els.testApiBtn.disabled = false;
    els.testApiBtn.textContent = "测试连接";
  }
}

els.providerSelect.addEventListener("change", () => fillProviderPreset(els.providerSelect.value));
els.saveApiBtn.addEventListener("click", saveApiConfig);
els.testApiBtn.addEventListener("click", testApiConnection);

(async () => {
  await loadApiConfig();
  renderApiForm();
})();

els.uploadBtn.addEventListener("click", () => els.resumeFile.click());
els.resumeFile.addEventListener("change", () => {
  const file = els.resumeFile.files && els.resumeFile.files[0];
  if (file) uploadResume(file);
});
els.saveResumeBtn.addEventListener("click", saveResume);
loadResume();

els.button.addEventListener("click", analyze);
els.interviewBtn.addEventListener("click", generateInterview);

els.tabJobBtn.addEventListener("click", () => switchTab("job"));
els.tabChatBtn.addEventListener("click", () => switchTab("chat"));
els.readChatBtn.addEventListener("click", readAndSuggest);
els.manualSubmitBtn.addEventListener("click", suggestFromManual);
switchTab("job");
