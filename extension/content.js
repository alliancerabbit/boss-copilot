/**
 * 当前页面岗位信息提取。
 *
 * 兜底优先：只要页面选区不为空就优先用选区 —— 网站改版最先挂掉的是选择器，
 * 而"用户能手动选中 JD"这件事永远成立，功能因此不会整体失效。
 * 只有当选区过短（比如只选中了一个词）时，才退回页面自动抓取。
 *
 * 通过 chrome.scripting.executeScript 注入时，本 IIFE 的返回值即为提取结果，
 * 同时挂到 window.__bossCopilotExtract 上供二次调用。
 */
(() => {
  const MIN_JD_LEN = 10;
  const MAX_JD_LEN = 20000;
  const MIN_PAGE_DESC_LEN = 20;

  // Boss 直聘详情页的候选选择器，从精确到宽泛排列
  const TITLE_SELECTORS = [
    ".job-banner .name h1",
    ".job-banner .job-name",
    ".job-banner h1",
    ".job-primary .name",
    "h1.job-name",
    ".job-name",
    "h1",
  ];

  const DESC_SELECTORS = [
    ".job-detail-section .job-sec-text",
    ".job-detail-section .desc",
    ".job-detail .text",
    ".job-sec .text",
    ".job-detail-section",
    ".job-detail",
    ".job-sec",
  ];

  const DESC_KEYWORDS = ["岗位职责", "职位描述", "工作职责", "任职要求", "岗位要求", "职位要求"];

  const clean = (text) =>
    (text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\r\n/g, "\n")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();

  /** 读取用户当前选区；折叠的空选区视为没有选中。 */
  const readSelection = () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return "";
    return clean(selection.toString());
  };

  /** 按选择器优先级找第一个"够长"的文本。 */
  const pickBySelectors = (selectors, minLen) => {
    for (const selector of selectors) {
      let nodes;
      try {
        nodes = document.querySelectorAll(selector);
      } catch {
        continue;
      }
      for (const node of nodes) {
        const text = clean(node.innerText || node.textContent);
        if (text.length >= minLen) return text;
      }
    }
    return "";
  };

  /** 从 <title> 里反推岗位名，例如「Java开发-上海-BOSS直聘」。 */
  const titleFromDocument = () => {
    const raw = clean(document.title);
    return raw
      .replace(/「[^」]*」/g, "")
      .replace(/[-—_|｜]\s*(BOSS|Boss|boss)?\s*直聘.*$/, "")
      .replace(/[-—_|｜]\s*招聘.*$/, "")
      .trim();
  };

  /** 选择器全部失效时的兜底：找同时命中多个 JD 关键词、且文本最长的容器。 */
  const descFromHeuristic = () => {
    let best = "";
    const nodes = document.querySelectorAll("div, section, article, main");
    for (const node of nodes) {
      const text = clean(node.innerText || "");
      if (text.length < 60 || text.length > 8000) continue;
      const hits = DESC_KEYWORDS.filter((keyword) => text.includes(keyword)).length;
      if (hits >= 2 && text.length > best.length) best = text;
    }
    return best;
  };

  const extract = () => {
    const selection = readSelection();
    const pageTitle = pickBySelectors(TITLE_SELECTORS, 2);
    const pageDesc =
      pickBySelectors(DESC_SELECTORS, MIN_PAGE_DESC_LEN) || descFromHeuristic();

    let jdText = "";
    let source = "none";

    if (selection.length >= MIN_JD_LEN) {
      jdText = selection;
      source = "selection";
    } else if (pageDesc) {
      jdText = pageDesc;
      source = "page";
    } else if (selection) {
      // 选区太短也先回传，让侧边栏给出明确提示，而不是静默失败
      jdText = selection;
      source = "selection-short";
    }

    const jobTitle = pageTitle || titleFromDocument();

    return {
      job_title: (jobTitle || "未命名岗位").slice(0, 120),
      jd_text: jdText.slice(0, MAX_JD_LEN),
      source,
      selection_len: selection.length,
      jd_len: jdText.length,
      page_url: location.href,
    };
  };

  window.__bossCopilotExtract = extract;
  return extract();
})();
