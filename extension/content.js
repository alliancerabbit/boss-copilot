/**
 * 当前页面岗位信息提取。
 *
 * 三层策略，优先级从高到低：
 *   1) 选区 —— 用户手动选中的文本永远最可信，网站改版也挡不住。
 *   2) 右侧详情区 —— 搜索结果页左侧是岗位列表，右侧才是当前打开的岗位。
 *   3) 启发式 —— 单页详情且选择器失效时，找命中多个 JD 关键词的最大文本块。
 *
 * 关键点：所有候选节点都要过可见性检查。
 * Boss 的列表页会为每个岗位预渲染一份 display:none 的详情块，直接取
 * querySelectorAll 的第一个，标题和 JD 永远是列表最上面那个岗位——
 * 这就是"只抓取最上面那个工作"的根因。
 *
 * 注入方式：chrome.scripting.executeScript 执行本 IIFE，返回值即提取结果，
 * 同时挂到 window.__bossCopilotExtract 上供二次调用。
 */
(() => {
  const MIN_JD_LEN = 10;
  const MAX_JD_LEN = 20000;
  const MIN_DESC_LEN = 20;

  // 详情区容器（从精确到宽泛）
  const DETAIL_PANES = [
    ".job-detail-container",
    ".job-detail-box",
    ".job-detail",
    '[class*="job-detail"]',
  ];

  // 详情区内的标题
  const TITLE_SELECTORS = [
    ".job-banner .name h1",
    ".job-banner .job-name",
    ".job-banner h1",
    '[class*="job-banner"] h1',
    ".job-name",
    "h1",
    "h2",
  ];

  // 详情区内的 JD 正文
  const DESC_SELECTORS = [
    ".job-detail-section .job-sec-text",
    ".job-sec-text",
    ".job-detail-section .desc",
    ".job-detail-section",
    ".job-sec",
    ".desc",
  ];

  // 列表卡片与"当前选中"的卡片
  const CARD_SELECTORS = ['[class*="job-card"]', ".job-list-box li"];
  const ACTIVE_CARD_SELECTORS = [
    ".job-card-wrapper.active",
    '[class*="job-card"][class*="active"]',
    '.job-list-box li.active',
    '[class*="active"][class*="job-card"]',
  ];

  const DESC_KEYWORDS = ["岗位职责", "职位描述", "工作职责", "任职要求", "岗位要求", "职位要求"];

  const clean = (text) =>
    (text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\r\n/g, "\n")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();

  const textOf = (el) => clean(el && (el.innerText || el.textContent));

  /** 元素是否真的显示在页面上。隐藏子树里的节点宽高为 0，这里会直接滤掉。 */
  const isVisible = (el) => {
    if (!el || typeof el.getBoundingClientRect !== "function") return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  };

  const visibleMatches = (selector, root = document) => {
    try {
      return Array.from(root.querySelectorAll(selector)).filter(isVisible);
    } catch {
      return [];
    }
  };

  /** 在给定根节点内，按选择器顺序找第一个"够长且可见"的文本。 */
  const pickIn = (root, selectors, minLen) => {
    for (const selector of selectors) {
      for (const el of visibleMatches(selector, root)) {
        const text = textOf(el);
        if (text.length >= minLen) return { el, text };
      }
    }
    return null;
  };

  /** 找可见的详情区：文案最长的那一个，且不能在左侧列表里。 */
  const findDetailPane = () => {
    let best = null;
    let bestLen = 0;
    for (const selector of DETAIL_PANES) {
      for (const el of visibleMatches(selector)) {
        if (el.closest('[class*="job-list"]')) continue;
        const len = textOf(el).length;
        if (len > bestLen) {
          best = el;
          bestLen = len;
        }
      }
    }
    return best;
  };

  const readSelection = () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return "";
    return clean(selection.toString());
  };

  const titleFromDocument = () => {
    const raw = clean(document.title);
    return raw
      .replace(/「[^」]*」/g, "")
      .replace(/[-—_|｜]\s*(BOSS|Boss|boss)?\s*直聘.*$/, "")
      .replace(/[-—_|｜]\s*招聘.*$/, "")
      .trim();
  };

  /** 列表里当前选中的那张卡片（详情区不可用时的次选）。 */
  const activeCardTitle = () => {
    for (const selector of ACTIVE_CARD_SELECTORS) {
      for (const card of visibleMatches(selector)) {
        const titleEl = visibleMatches(
          ".job-name, [class*='job-name'], h3, h4",
          card
        )[0];
        const text = textOf(titleEl || card);
        if (text.length >= 2) return text.split("\n")[0];
      }
    }
    return "";
  };

  /**
   * 全页找标题，但排除列表卡片。
   * 详情容器常常只命中正文区（如 .job-detail-section），标题在它上面的 banner 里，
   * 所以在 pane 里找不到时要放宽到全页——同时必须排除卡片，否则又会抓到第一个岗位。
   */
  const titleOutsideList = () => {
    for (const selector of TITLE_SELECTORS) {
      for (const el of visibleMatches(selector)) {
        if (el.closest('[class*="job-card"], [class*="job-list"]')) continue;
        const text = textOf(el);
        if (text.length >= 2) return text.split("\n")[0];
      }
    }
    return "";
  };

  const countJobCards = () => {
    const seen = new Set();
    for (const selector of CARD_SELECTORS) {
      visibleMatches(selector).forEach((el) => seen.add(el));
    }
    return seen.size;
  };

  /** 启发式：找同时命中多个 JD 关键词、且不含列表卡片的可见大文本块。 */
  const descFromHeuristic = () => {
    let best = "";
    const nodes = document.querySelectorAll("div, section, article, main");
    for (const node of nodes) {
      if (!isVisible(node)) continue;
      // 既不落在某张卡片里，自身也不包含卡片列表——排除整页外壳
      if (node.closest('[class*="job-card"]')) continue;
      if (node.querySelector('[class*="job-card"]')) continue;
      const text = textOf(node);
      if (text.length < 60 || text.length > 8000) continue;
      const hits = DESC_KEYWORDS.filter((keyword) => text.includes(keyword)).length;
      if (hits >= 2 && text.length > best.length) best = text;
    }
    return best;
  };

  const extract = () => {
    const selection = readSelection();
    const cards = countJobCards();
    const pane = findDetailPane();

    const paneTitle = pane ? (pickIn(pane, TITLE_SELECTORS, 2) || {}).text || "" : "";
    const paneDesc = pane ? (pickIn(pane, DESC_SELECTORS, MIN_DESC_LEN) || {}).text || "" : "";

    let jdText = "";
    let source = "none";

    if (selection.length >= MIN_JD_LEN) {
      jdText = selection;
      source = "selection";
    } else if (paneDesc) {
      jdText = paneDesc;
      source = "page-detail";
    } else {
      // 详情区选择器没命中时再退启发式：它已排除卡片和整页外壳，
      // 在列表页上不会把最上面那个岗位当答案
      const heuristic = descFromHeuristic();
      if (heuristic) {
        jdText = heuristic;
        source = cards > 1 ? "page-detail" : "page";
      } else if (selection) {
        jdText = selection;
        source = "selection-short";
      } else if (cards > 1) {
        // 列表页且找不到详情：宁可不猜，也不要分析错的岗位
        source = "list-no-detail";
      }
    }

    const jobTitle = paneTitle || titleOutsideList() || activeCardTitle() || titleFromDocument();

    return {
      job_title: (jobTitle || "未命名岗位").slice(0, 120),
      jd_text: jdText.slice(0, MAX_JD_LEN),
      source,
      job_count: cards,
      selection_len: selection.length,
      jd_len: jdText.length,
      page_url: location.href,
    };
  };

  window.__bossCopilotExtract = extract;
  return extract();
})();
