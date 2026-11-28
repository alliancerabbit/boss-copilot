/**
 * Boss 直聘聊天页读取 / 填入桥接。
 *
 * 只在用户点击侧边栏按钮时由 chrome.scripting.executeScript 注入，
 * 不做后台监听、不轮询、不自动发送。
 *
 * 两个能力：
 *   read()      读取顶部岗位信息 + 最近 15 条消息（区分 hr / me / system）
 *   fill(text)  只把文字填进输入框，绝不点击发送按钮
 *
 * Boss 的类名是混淆且会变的，所以每一层都有兜底：
 *   消息容器 → 多条选择器；角色 → 类名 + 气泡左右位置；正文 → 多层子选择器。
 * 任何一层失败都不抛异常，返回 {ok:false, reason}，由侧边栏引导用户手动粘贴。
 */
(() => {
  const MAX_MESSAGES = 15;
  const MIN_TEXT_LEN = 1;

  const clean = (text) =>
    (text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\r\n/g, "\n")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{2,}/g, "\n")
      .trim();

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

  const RECORD_SELECTORS = [
    ".chat-record",
    ".chat-conversation .chat-record",
    '[class*="chat-record"]',
    '[class*="message-list"]',
    '[class*="chat-content"]',
  ];

  const MESSAGE_SELECTORS = [
    ".message-item",
    '[class*="message-item"]',
    ".item-friend, .item-mine, .item-system",
  ];

  const TITLE_SELECTORS = [
    ".chat-job .job-name",
    ".chat-job .name",
    ".chat-header .job-name",
    '[class*="chat-job"] [class*="job-name"]',
    ".job-name",
  ];

  const SALARY_SELECTORS = [
    ".chat-job .salary",
    ".chat-job .job-salary",
    '[class*="chat-job"] [class*="salary"]',
    ".job-salary",
    '[class*="salary"]',
  ];

  const COMPANY_SELECTORS = [
    ".chat-job .company",
    ".chat-job .company-name",
    ".chat-header .company-name",
    '[class*="chat-job"] [class*="company"]',
    ".company-name",
  ];

  const INPUT_SELECTORS = [
    ".chat-input[contenteditable='true']",
    ".chat-editor [contenteditable='true']",
    ".chat-input textarea",
    "textarea.chat-input",
    '[class*="chat-input"] [contenteditable="true"]',
    '[class*="chat-input"] textarea',
    "[contenteditable='true']",
    "textarea",
  ];

  const pickText = (selectors, minLen = 2, root = document) => {
    for (const selector of selectors) {
      for (const el of visibleMatches(selector, root)) {
        const text = clean(el.innerText || el.textContent);
        if (text.length >= minLen && text.length <= 200) return text;
      }
    }
    return "";
  };

  const findRecord = () => {
    let best = null;
    let bestCount = 0;
    for (const selector of RECORD_SELECTORS) {
      for (const el of visibleMatches(selector)) {
        const count = el.querySelectorAll('[class*="message-item"], .item-friend, .item-mine').length;
        if (count > bestCount) {
          best = el;
          bestCount = count;
        }
      }
    }
    return best;
  };

  /** 角色判断：先看类名，类名不可靠时用气泡在容器的左/右位置兜底。 */
  const roleOf = (node, record) => {
    const own = String(node.className || "");
    const parent = node.parentElement ? String(node.parentElement.className || "") : "";
    const signature = `${own} ${parent}`;

    if (/item-system|message-system|system|tips|notice/i.test(signature)) return "system";
    if (/item-mine|message-mine|\bmine\b|self|item-right/i.test(signature)) return "me";
    if (/item-friend|message-friend|\bfriend\b|other|item-left/i.test(signature)) return "hr";

    const rect = node.getBoundingClientRect();
    const box = record ? record.getBoundingClientRect() : null;
    if (box && box.width > 0) {
      const center = rect.left + rect.width / 2;
      return center > box.left + box.width * 0.55 ? "me" : "hr";
    }
    return "hr";
  };

  const textOfMessage = (node) => {
    const selectors = [
      ".text",
      ".message-text",
      ".content",
      '[class*="message-text"]',
      '[class*="text"]',
      '[class*="content"]',
      "p",
    ];
    for (const selector of selectors) {
      for (const el of node.querySelectorAll(selector)) {
        if (!isVisible(el)) continue;
        const text = clean(el.innerText || el.textContent);
        if (text.length >= MIN_TEXT_LEN) return text;
      }
    }
    return clean(node.innerText || node.textContent);
  };

  const readMessages = (record) => {
    const scopes = record ? [record, document] : [document];
    const combined = MESSAGE_SELECTORS.join(",");

    for (const scope of scopes) {
      const found = visibleMatches(combined, scope);
      if (!found.length) continue;

      // 去掉"包含其它候选节点"的外层容器，只留最内层的气泡；顺序保持 DOM 顺序
      const nodes = found.filter((el) => !found.some((other) => other !== el && el.contains(other)));
      const messages = [];
      for (const node of nodes) {
        const content = textOfMessage(node);
        if (!content) continue;
        messages.push({ role: roleOf(node, record), content });
      }
      if (messages.length) return messages.slice(-MAX_MESSAGES);
    }
    return [];
  };

  const headerLines = () => {
    // 以薪资为锚点向上找 3~4 层：那一块通常就是"岗位名 / 公司 / 薪资"的头部
    const anchor = visibleMatches(SALARY_SELECTORS.join(","))[0];
    if (!anchor) return [];
    let box = anchor;
    for (let i = 0; i < 4 && box.parentElement; i += 1) box = box.parentElement;
    return clean(box.innerText || box.textContent)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  };

  const jobInfo = () => {
    const lines = headerLines();
    const titlePattern = /工程师|开发|产品|设计|运营|经理|主管|总监|架构师|专员|顾问|分析师|实习生/;
    const companyPattern = /公司|集团|科技|有限|银行|证券|研究院|工作室/;
    const salaryPattern = /\d+\s*[-~—]\s*\d+\s*[Kk千万]/;

    const title =
      pickText(TITLE_SELECTORS) ||
      lines.find((line) => line.length <= 30 && titlePattern.test(line)) ||
      "";
    const company =
      pickText(COMPANY_SELECTORS) ||
      lines.find((line) => line !== title && line.length <= 40 && companyPattern.test(line)) ||
      "";
    const salary =
      pickText(SALARY_SELECTORS) || lines.find((line) => salaryPattern.test(line)) || "";

    return { title, salary, company };
  };

  const read = () => {
    try {
      if (!/\/web\/geek\/chat/.test(location.pathname)) {
        return {
          ok: false,
          reason: "not-chat-page",
          page: location.pathname,
          job_info: jobInfo(),
        };
      }

      const record = findRecord();
      const messages = readMessages(record);
      if (!messages.length) {
        return {
          ok: false,
          reason: record ? "no-messages" : "no-record",
          job_info: jobInfo(),
          page: location.pathname,
        };
      }

      return {
        ok: true,
        job_info: jobInfo(),
        messages,
        count: messages.length,
        page: location.pathname,
      };
    } catch (error) {
      return { ok: false, reason: "read-error", error: String(error), job_info: {} };
    }
  };

  const findInput = () => {
    let candidates = [];
    for (const selector of INPUT_SELECTORS) {
      candidates = visibleMatches(selector).filter((el) => !el.disabled && !el.readOnly);
      if (candidates.length) break;
    }
    if (!candidates.length) return null;
    // 聊天输入框一定在最下方：同组里取 bottom 最大的，避开页面上方的搜索框
    candidates.sort(
      (a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom
    );
    return candidates[0];
  };

  /** 只填字，不点发送。 */
  const fill = (text) => {
    try {
      const value = String(text || "");
      if (!value) return { ok: false, reason: "empty-text" };

      const el = findInput();
      if (!el) return { ok: false, reason: "input-not-found" };

      el.focus();

      if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
        const proto =
          el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
        setter.call(el, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.selectionStart = el.value.length;
        return { ok: true, tag: el.tagName };
      }

      // contenteditable：先全选再插入，避免把新内容拼在旧草稿后面
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      selection.removeAllRanges();
      selection.addRange(range);

      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, value);
      } catch {
        inserted = false;
      }
      if (!inserted) {
        el.textContent = value;
        el.dispatchEvent(
          new InputEvent("input", { bubbles: true, inputType: "insertText", data: value })
        );
      }
      return { ok: true, tag: el.tagName };
    } catch (error) {
      return { ok: false, reason: "fill-error", error: String(error) };
    }
  };

  window.__bossCopilotChat = { read, fill };
  return { ok: true, href: location.href };
})();
