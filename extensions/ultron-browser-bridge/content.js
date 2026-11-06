/* Ultron Browser Bridge — page-side DOM scanner and actor.
 *
 * Assigns stable @b1…@bN refs to visible interactive elements, extracts the important
 * content for WhatsApp / Gemini / Claude / ChatGPT / YouTube / Gmail, and executes
 * click/type/scroll/read commands from background.js. No network access here.
 */

(() => {
  if (window.__ultronBridgeLoaded) return;
  window.__ultronBridgeLoaded = true;

  const MAX_ELEMENTS = 120;
  const SELECTOR = [
    "button",
    "a[href]",
    "input",
    "textarea",
    "select",
    "[contenteditable='true']",
    "[role='button']",
    "[role='tab']",
    "[role='textbox']",
    "[role='link']",
    "[role='menuitem']",
    "[aria-label]",
    "[data-testid]",
  ].join(",");

  let lastElements = [];

  function visible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) {
      return false;
    }
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight + 600;
  }

  function labelFor(el) {
    const aria = el.getAttribute("aria-label") || "";
    const placeholder = el.getAttribute("placeholder") || "";
    const title = el.getAttribute("title") || "";
    const alt = el.getAttribute("alt") || "";
    const name = el.getAttribute("name") || "";
    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return (aria || placeholder || title || alt || text || name).slice(0, 180);
  }

  function roleFor(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    if (el.tagName === "A") return "link";
    if (el.tagName === "BUTTON") return "button";
    if (el.tagName === "TEXTAREA") return "textbox";
    if (el.tagName === "SELECT") return "combobox";
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return type === "search" ? "searchbox" : "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return el.tagName.toLowerCase();
  }

  function elementRecord(el, index) {
    const rect = el.getBoundingClientRect();
    return {
      ref: `@b${index + 1}`,
      tag: el.tagName.toLowerCase(),
      role: roleFor(el),
      label: labelFor(el),
      text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200),
      placeholder: el.getAttribute("placeholder") || "",
      aria: el.getAttribute("aria-label") || "",
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      editable: Boolean(el.isContentEditable) || ["INPUT", "TEXTAREA"].includes(el.tagName),
    };
  }

  function scanElements() {
    const elements = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(SELECTOR)) {
      if (elements.length >= MAX_ELEMENTS) break;
      if (seen.has(el) || !visible(el)) continue;
      if (!labelFor(el) && !el.isContentEditable) continue;
      seen.add(el);
      elements.push(elementRecord(el, elements.length));
    }
    lastElements = elements;
    return elements;
  }

  function byText(texts) {
    const wanted = texts.map((item) => item.toLowerCase());
    const candidates = Array.from(document.querySelectorAll("button, a, [role='button'], [role='tab'], [aria-label]"));
    return candidates.find((el) => {
      const label = labelFor(el).toLowerCase();
      return wanted.some((needle) => label === needle || label.includes(needle));
    }) || null;
  }

  function firstVisible(selectors) {
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el)) return el;
      }
    }
    return null;
  }

  function textOf(selectors, limit = 12) {
    const rows = [];
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el)) continue;
        const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (text && !rows.includes(text)) rows.push(text.slice(0, 500));
        if (rows.length >= limit) break;
      }
      if (rows.length >= limit) break;
    }
    return rows;
  }

  function specialExtract() {
    const host = location.hostname.replace(/^www\./, "");
    const special = { host, text: "", new_chat: "", prompt: "", send: "", latest: [], items: [] };

    if (host === "web.whatsapp.com") {
      special.items = textOf(
        [
          "[data-testid='cell-frame-container']",
          "[role='listitem']",
          "#pane-side [role='listitem']",
        ],
        30
      );
      special.latest = textOf(
        [".message-in", ".message-out", "div[data-id]", "[data-testid='msg-container']"],
        12
      );
      special.unread = textOf(
        ["[aria-label*='unread' i]", "span[data-icon='unread-count']", "[data-testid='icon-unread-count']"],
        20
      );
      special.text = textOf(["[data-testid='conversation-panel-body']", "#main"], 1)[0] || "";
      special.prompt = "div[contenteditable='true']";
      special.send = "button[aria-label*='Send' i]";
      return special;
    }

    if (host.includes("gemini.google.com")) {
      special.new_chat = "New chat";
      special.prompt = "rich-textarea [contenteditable='true'], textarea";
      special.send = "button[aria-label*='Send' i], button[aria-label*='Submit' i]";
      special.latest = textOf(["model-response", "message-content", ".model-response-text", ".markdown"], 8);
      special.text = textOf(["main"], 1)[0] || "";
    } else if (host.includes("claude.ai")) {
      special.new_chat = "New chat";
      special.prompt = "div[contenteditable='true'], textarea";
      special.send = "button[aria-label*='Send' i]";
      special.latest = textOf([".font-claude-message", "[data-testid='chat-message']", ".prose"], 8);
      special.text = textOf(["main"], 1)[0] || "";
    } else if (host.includes("chatgpt.com") || host.includes("chat.openai.com")) {
      special.new_chat = "New chat";
      special.prompt = "#prompt-textarea, textarea";
      special.send = "[data-testid='send-button'], button[aria-label*='Send' i]";
      special.latest = textOf(["[data-message-author-role='assistant']", ".markdown"], 8);
      special.text = textOf(["main"], 1)[0] || "";
    } else if (host.includes("youtube.com")) {
      special.prompt = "input#search, input[name='search_query']";
      special.send = "button#search-icon-legacy, button[aria-label*='Search' i]";
      special.items = textOf(["ytd-video-renderer", "ytd-rich-item-renderer", "ytd-compact-video-renderer"], 20);
      special.show_more = "tp-yt-paper-button#expand, #expand";
      special.play_pause = "button.ytp-play-button";
    } else if (host.includes("mail.google.com")) {
      special.items = textOf(["tr.zA.zE", "tr.zA"], 25);
      special.compose = "Compose";
      special.text = textOf(["div[role='main']"], 1)[0] || "";
    } else if (host.includes("github.com")) {
      special.items = textOf(["article", ".Box-row", "[data-testid='results-list'] > div"], 20);
    }

    if (!special.text) {
      special.text = (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 8000);
    }
    return special;
  }

  function snapshot() {
    return {
      url: location.href,
      title: document.title,
      elements: scanElements(),
      special: specialExtract(),
    };
  }

  function resolveTarget(target) {
    const wanted = (target || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!wanted) return null;
    if (/^@b\d+$/i.test(wanted)) {
      const index = Number(wanted.slice(2)) - 1;
      if (lastElements[index]) {
        const all = Array.from(document.querySelectorAll(SELECTOR)).filter(visible);
        return all[index] || null;
      }
    }
    let best = null;
    let bestScore = 0;
    for (const el of document.querySelectorAll(SELECTOR)) {
      if (!visible(el)) continue;
      const label = labelFor(el).toLowerCase();
      const text = (el.innerText || "").replace(/\s+/g, " ").trim().toLowerCase();
      const placeholder = (el.getAttribute("placeholder") || "").toLowerCase();
      let score = 0;
      if (label === wanted || text === wanted || placeholder === wanted) score = 100;
      else if (label.includes(wanted) || text.includes(wanted) || placeholder.includes(wanted)) score = 70;
      else if (wanted.includes(label) && label.length > 2) score = 40;
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return bestScore >= 40 ? best : null;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value") && Object.getOwnPropertyDescriptor(proto, "value").set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function typeInto(el, text) {
    el.focus();
    if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, text);
      } catch (_error) {
        inserted = false;
      }
      if (!inserted) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
      }
      return true;
    }
    setNativeValue(el, text);
    return true;
  }

  function pressEnter(el) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    }
  }

  function clickSend(special) {
    const button = special && special.send ? firstVisible([special.send]) : null;
    const fallback = button || byText(["send", "send message", "submit", "go"]);
    if (fallback) {
      fallback.focus();
      fallback.click();
      return true;
    }
    return false;
  }

  function actionClick(target) {
    const el = resolveTarget(target);
    if (!el) return { ok: false, error: `I couldn't find ${target || "that element"} on this page.` };
    el.scrollIntoView({ block: "center", inline: "center" });
    el.focus();
    el.click();
    return { ok: true, matched: { ref: elementRecord(el, 0).ref, label: labelFor(el) } };
  }

  function actionType(target, text, submit) {
    const special = specialExtract();
    let el = target ? resolveTarget(target) : null;
    if (!el && special.prompt) el = firstVisible([special.prompt]);
    if (!el) el = firstVisible(["textarea", "input[type='text']", "[contenteditable='true']", "input[type='search']"]);
    if (!el) return { ok: false, error: "I couldn't find a text field on this page." };
    typeInto(el, text || "");
    let sent = false;
    if (submit) {
      sent = clickSend(special);
      if (!sent) {
        pressEnter(el);
        sent = true;
      }
    }
    return { ok: true, matched: { ref: elementRecord(el, 0).ref, label: labelFor(el) }, submitted: sent };
  }

  function actionScroll(direction) {
    const delta = String(direction || "down").toLowerCase() === "up" ? -window.innerHeight * 0.9 : window.innerHeight * 0.9;
    window.scrollBy({ top: delta, behavior: "smooth" });
    return { ok: true, scrolled: delta > 0 ? "down" : "up" };
  }

  function actionRead() {
    const special = specialExtract();
    return { ok: true, text: special.text, special };
  }

  function handleAction(message) {
    switch (message.action) {
      case "click_dom":
        return actionClick(message.target);
      case "type_dom":
        return actionType(message.target, message.text, message.submit);
      case "scroll_dom":
        return actionScroll(message.text || "down");
      case "read_dom":
        return actionRead();
      default:
        return { ok: false, error: `unknown action ${message.action}` };
    }
  }

  const runtime = globalThis.browser && globalThis.browser.runtime ? globalThis.browser.runtime : chrome.runtime;
  runtime.onMessage.addListener((message, _sender, sendResponse) => {
    try {
      if (message.type === "ULTRON_SNAPSHOT") {
        sendResponse(snapshot());
      } else if (message.type === "ULTRON_ACTION") {
        sendResponse(handleAction(message));
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (error) {
      sendResponse({ ok: false, error: String(error && error.message ? error.message : error) });
    }
    return true;
  });
})();
