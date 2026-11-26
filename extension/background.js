/**
 * MV3 Service Worker。
 * 让点击工具栏图标直接打开当前标签页的侧边栏，而不是弹出 popup。
 * 该行为由 Chrome 持久化，但 Service Worker 可能被回收重启，所以模块顶层也设一次。
 */

const BEHAVIOR = { openPanelOnActionClick: true };

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior(BEHAVIOR).catch((error) => {
    console.error("[BossCopilot] setPanelBehavior 失败：", error);
  });
});

chrome.sidePanel.setPanelBehavior(BEHAVIOR).catch(() => {
  // 扩展刚安装、sidePanel API 尚未就绪时会走到这里，onInstalled 里会再设一次
});
