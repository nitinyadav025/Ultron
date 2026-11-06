/* Ultron Browser Bridge — local-only Brave/Firefox tab + DOM sync.
 *
 * Every 800 ms it pushes the open tabs and the active tab's DOM snapshot to
 * http://127.0.0.1:9932/api/browser/sync and executes the queued commands it gets back.
 * No data leaves the machine.
 */

const SERVER = "http://127.0.0.1:9932";
const POLL_MS = 800;
const EXT = typeof browser !== "undefined" ? browser : chrome;
const PROMISE_API = typeof browser !== "undefined";
const pendingResults = [];
let running = false;
let timer = null;

function call(fn, ...args) {
  if (PROMISE_API) return fn(...args);
  return new Promise((resolve, reject) => {
    try {
      fn(...args, (value) => {
        const error = chrome.runtime.lastError;
        if (error) reject(new Error(error.message));
        else resolve(value);
      });
    } catch (error) {
      reject(error);
    }
  });
}

function detectBrowser() {
  const ua = navigator.userAgent || "";
  if (ua.includes("Firefox/")) return "firefox";
  if (ua.includes("Edg/")) return "edge";
  if (navigator.brave || ua.includes("Brave")) return "brave";
  return "chrome";
}

const BROWSER = detectBrowser();

async function snapshotDom(tab) {
  if (!tab || !tab.id) return null;
  const message = { type: "ULTRON_SNAPSHOT", tabId: tab.id };
  try {
    const dom = await call(EXT.tabs.sendMessage, tab.id, message);
    return dom ? { ...dom, tabId: tab.id, url: tab.url || "" } : null;
  } catch (_error) {
    // A tab opened before the extension was installed has no content script yet.
    try {
      if (EXT.scripting && EXT.scripting.executeScript) {
        await call(EXT.scripting.executeScript, {
          target: { tabId: tab.id },
          files: ["content.js"],
        });
        const dom = await call(EXT.tabs.sendMessage, tab.id, message);
        return dom ? { ...dom, tabId: tab.id, url: tab.url || "" } : null;
      }
    } catch (error) {
      // Chrome's own pages, PDF viewers and the extension store cannot be scripted.
    }
    return null;
  }
}

async function collectTabs() {
  const raw = await call(EXT.tabs.query, {});
  const tabs = [];
  let active = null;
  for (const tab of raw || []) {
    const url = tab.url || "";
    if (!/^https?:/i.test(url)) continue;
    const entry = {
      tabId: tab.id,
      windowId: tab.windowId,
      title: tab.title || url,
      url,
      active: Boolean(tab.active),
      audible: Boolean(tab.audible),
      pinned: Boolean(tab.pinned),
      browser: BROWSER,
    };
    tabs.push(entry);
    if (tab.active) active = tab;
  }
  return { tabs, active };
}

async function executeCommand(command) {
  if (!command || !command.type) return { id: command && command.id, ok: false };
  try {
    switch (command.type) {
      case "switch_tab": {
        await call(EXT.tabs.update, command.tabId, { active: true });
        if (command.windowId) {
          await call(EXT.windows.update, command.windowId, { focused: true });
        }
        return { id: command.id, ok: true };
      }
      case "close_tab": {
        await call(EXT.tabs.remove, command.tabId);
        return { id: command.id, ok: true };
      }
      case "open_url": {
        const tab = await call(EXT.tabs.create, { url: command.text || command.target });
        return { id: command.id, ok: true, tabId: tab && tab.id };
      }
      case "click_dom":
      case "type_dom":
      case "read_dom":
      case "scroll_dom": {
        const result = await call(EXT.tabs.sendMessage, command.tabId, {
          type: "ULTRON_ACTION",
          action: command.type,
          target: command.target,
          text: command.text,
          submit: command.submit,
        });
        return { id: command.id, ok: true, result };
      }
      default:
        return { id: command.id, ok: false, error: `unknown command ${command.type}` };
    }
  } catch (error) {
    return { id: command.id, ok: false, error: String(error && error.message ? error.message : error) };
  }
}

async function syncOnce() {
  if (running) return;
  running = true;
  try {
    const { tabs, active } = await collectTabs();
    const dom = await snapshotDom(active);
    const response = await fetch(`${SERVER}/api/browser/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser: BROWSER,
        tabs,
        dom,
        results: pendingResults.splice(0, pendingResults.length),
      }),
    });
    if (response.ok) {
      const payload = await response.json();
      for (const command of payload.actions || []) {
        const result = await executeCommand(command);
        if (result) pendingResults.push(result);
      }
    }
  } catch (_error) {
    // The Ultron environment server is not running yet; retry on the next tick.
  } finally {
    running = false;
    timer = setTimeout(syncOnce, POLL_MS);
  }
}

function boot() {
  if (timer) clearTimeout(timer);
  syncOnce();
}

EXT.runtime.onStartup.addListener(boot);
EXT.runtime.onInstalled.addListener(boot);
boot();
