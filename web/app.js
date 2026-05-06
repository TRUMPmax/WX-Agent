const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const inputWrapEl = document.querySelector(".input-wrap");
const sendBtn = document.getElementById("send");
const quickButtons = document.querySelectorAll(".quick");
const titleEl = document.getElementById("chatTitle");
const subtitleEl = document.getElementById("chatSubtitle");
const providerEl = document.getElementById("modelProvider");
const modelSelectEl = document.getElementById("modelSelect");
const modelTriggerEl = modelSelectEl?.querySelector(".model-trigger");
const modelTriggerTextEl = modelSelectEl?.querySelector(".model-trigger-text");
const modelMenuEl = modelSelectEl?.querySelector(".model-menu");

const SESSION_ID_KEY = "wx_agent_session_id";
const SESSION_TOUCH_KEY = "wx_agent_session_touch";
const MODEL_PROVIDER_KEY = "wx_agent_model_provider";
const SESSION_IDLE_MS = 30 * 60 * 1000;
const NOT_AVAILABLE_SUFFIX = "（不可用）";

let busy = false;

function createSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID().replace(/-/g, "");
  }
  return `sid_${Date.now()}_${Math.random().toString(16).slice(2, 10)}`;
}

function updateSessionId(sessionId) {
  if (!sessionId) return;
  localStorage.setItem(SESSION_ID_KEY, sessionId);
  localStorage.setItem(SESSION_TOUCH_KEY, String(Date.now()));
}

function getSessionId() {
  const now = Date.now();
  let sessionId = localStorage.getItem(SESSION_ID_KEY) || "";
  const lastTouchRaw = localStorage.getItem(SESSION_TOUCH_KEY) || "0";
  const lastTouch = Number.parseInt(lastTouchRaw, 10) || 0;
  if (!sessionId || now - lastTouch > SESSION_IDLE_MS) {
    sessionId = createSessionId();
  }
  updateSessionId(sessionId);
  return sessionId;
}

function cleanProviderLabel(label) {
  return String(label || "").replace(NOT_AVAILABLE_SUFFIX, "");
}

function thinkingMarkup() {
  return `
    <span class="thinking-label">正在思考</span>
    <span class="thinking-dots" aria-hidden="true">
      <span></span><span></span><span></span>
    </span>
  `;
}

function addMessage(role, text, streaming = false) {
  const div = document.createElement("div");
  div.className = `msg ${role}${streaming ? " streaming" : ""}`;
  if (streaming && role === "assistant" && !text) {
    div.classList.add("thinking");
    div.innerHTML = thinkingMarkup();
  } else {
    div.textContent = text;
  }
  messagesEl.appendChild(div);
  scrollToBottom();
  return div;
}

function removeThinking(el) {
  if (!el?.classList.contains("thinking")) return;
  el.classList.remove("thinking");
  el.textContent = "";
}

function appendTypedText(el, text) {
  if (!el || !text) return;
  removeThinking(el);
  el._typingQueue = `${el._typingQueue || ""}${text}`;
  if (el._typingActive) return;
  el._typingActive = true;
  drainTypedText(el);
}

function drainTypedText(el) {
  if (!el) return;
  const queue = el._typingQueue || "";
  if (!queue.length) {
    el._typingActive = false;
    if (el._finishWhenTyped) {
      el.classList.remove("streaming");
      el._finishWhenTyped = false;
    }
    return;
  }

  const step = Math.max(1, Math.ceil(queue.length / 28));
  el.textContent += queue.slice(0, step);
  el._typingQueue = queue.slice(step);
  scrollToBottom();
  window.setTimeout(() => drainTypedText(el), 18);
}

function finishAssistant(el, fallbackText = "") {
  if (!el) return;
  removeThinking(el);
  if (!el.textContent.trim() && !(el._typingQueue || "").trim() && fallbackText) {
    appendTypedText(el, fallbackText);
  }
  if (el._typingActive || (el._typingQueue || "").length) {
    el._finishWhenTyped = true;
    return;
  }
  el.classList.remove("streaming");
}

function setAssistantError(el, text) {
  if (!el) return;
  el._typingQueue = "";
  el._typingActive = false;
  el._finishWhenTyped = false;
  el.classList.remove("thinking", "streaming");
  el.textContent = text;
  scrollToBottom();
}

function scrollToBottom() {
  window.requestAnimationFrame(() => {
    messagesEl.scrollTo({
      top: messagesEl.scrollHeight,
      behavior: "smooth",
    });
  });
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 168)}px`;
}

function updateInputState() {
  inputWrapEl?.classList.toggle("has-value", Boolean(inputEl.value.trim()));
}

function setBusy(nextBusy) {
  busy = nextBusy;
  sendBtn.disabled = nextBusy;
  inputEl.disabled = nextBusy;
  if (providerEl) providerEl.disabled = nextBusy;
  if (modelTriggerEl) modelTriggerEl.disabled = nextBusy;
  if (nextBusy) {
    closeModelMenu();
  }
}

function parseSSE(buffer) {
  const events = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() || "";
  for (const block of blocks) {
    const lines = block.split("\n");
    let event = "message";
    let data = "";
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) continue;
    try {
      events.push({ event, data: JSON.parse(data) });
    } catch (_) {
      events.push({ event, data: { text: data } });
    }
  }
  return { events, rest };
}

function selectedProvider() {
  if (!providerEl || !providerEl.value) return "ollama";
  return providerEl.value;
}

function updateProviderBadge(providerLabel) {
  if (!subtitleEl) return;
  if (providerLabel) {
    subtitleEl.textContent = `当前模型：${cleanProviderLabel(providerLabel)}（流式回复）`;
    return;
  }
  subtitleEl.textContent = "当前为流式回复模式";
}

function currentProviderOption() {
  if (!providerEl || providerEl.selectedIndex < 0) return null;
  return providerEl.options[providerEl.selectedIndex] || null;
}

function refreshModelSelect() {
  if (!providerEl || !modelTriggerTextEl || !modelMenuEl) return;
  const current = currentProviderOption();
  modelTriggerTextEl.textContent = current ? cleanProviderLabel(current.textContent) : "选择模型";
  modelMenuEl.innerHTML = "";

  for (const option of Array.from(providerEl.options)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `model-option${option.value === providerEl.value ? " is-selected" : ""}`;
    btn.setAttribute("role", "option");
    btn.setAttribute("aria-selected", option.value === providerEl.value ? "true" : "false");
    btn.disabled = option.disabled;
    btn.textContent = cleanProviderLabel(option.textContent);
    btn.addEventListener("click", () => {
      if (option.disabled) return;
      providerEl.value = option.value;
      providerEl.dispatchEvent(new Event("change", { bubbles: true }));
      closeModelMenu();
    });
    modelMenuEl.appendChild(btn);
  }
}

function openModelMenu() {
  if (busy || !modelSelectEl) return;
  modelSelectEl.classList.add("open");
  modelTriggerEl?.setAttribute("aria-expanded", "true");
}

function closeModelMenu() {
  modelSelectEl?.classList.remove("open");
  modelTriggerEl?.setAttribute("aria-expanded", "false");
}

function toggleModelMenu() {
  if (modelSelectEl?.classList.contains("open")) {
    closeModelMenu();
  } else {
    openModelMenu();
  }
}

function setProviderSelection(providerId) {
  if (!providerEl || !providerId) return;
  const option = Array.from(providerEl.options).find((op) => op.value === providerId && !op.disabled);
  if (!option) return;
  providerEl.value = providerId;
  localStorage.setItem(MODEL_PROVIDER_KEY, providerId);
  refreshModelSelect();
}

function fillProviderOptions(meta) {
  if (!providerEl) return;
  providerEl.innerHTML = "";

  const items = Array.isArray(meta?.items) ? meta.items : [];
  if (!items.length) {
    const op = document.createElement("option");
    op.value = "ollama";
    op.textContent = "Qwen 本地模型";
    providerEl.appendChild(op);
    providerEl.value = "ollama";
    localStorage.setItem(MODEL_PROVIDER_KEY, "ollama");
    updateProviderBadge(op.textContent);
    refreshModelSelect();
    return;
  }

  for (const item of items) {
    const op = document.createElement("option");
    op.value = item.id;
    op.disabled = item.available === false;
    op.textContent = item.available === false ? `${item.label}${NOT_AVAILABLE_SUFFIX}` : item.label;
    providerEl.appendChild(op);
  }

  const saved = localStorage.getItem(MODEL_PROVIDER_KEY) || "";
  const next = saved || meta.default || "ollama";
  setProviderSelection(next);

  if (!providerEl.value) {
    const firstEnabled = Array.from(providerEl.options).find((op) => !op.disabled);
    if (firstEnabled) providerEl.value = firstEnabled.value;
  }

  if (providerEl.value) {
    localStorage.setItem(MODEL_PROVIDER_KEY, providerEl.value);
    const currentOption = currentProviderOption();
    updateProviderBadge(currentOption ? currentOption.textContent : "");
  }
  refreshModelSelect();
}

async function loadProviders() {
  try {
    const resp = await fetch("/api/model-providers", { method: "GET" });
    if (!resp.ok) throw new Error("provider api failed");
    const data = await resp.json();
    fillProviderOptions(data);
  } catch (_) {
    fillProviderOptions({
      items: [{ id: "ollama", label: "Qwen 本地模型", available: true }],
      default: "ollama",
    });
  }
}

function showSkeletonScreen() {
  messagesEl.innerHTML = "";
  const skeleton = document.createElement("div");
  skeleton.className = "skeleton-wrap";
  skeleton.setAttribute("aria-hidden", "true");
  skeleton.innerHTML = `
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
  `;
  messagesEl.appendChild(skeleton);
}

function hideSkeletonScreen() {
  const skeleton = messagesEl.querySelector(".skeleton-wrap");
  skeleton?.remove();
}

async function ask(message) {
  if (!message || busy) return;
  hideSkeletonScreen();
  setBusy(true);
  addMessage("user", message);
  const assistant = addMessage("assistant", "", true);
  const sessionId = getSessionId();
  const provider = selectedProvider();

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId, model_provider: provider }),
    });

    if (!resp.ok || !resp.body) {
      setAssistantError(assistant, "请求失败，请稍后重试。");
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let sseBuffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });
      const parsed = parseSSE(sseBuffer);
      sseBuffer = parsed.rest;

      for (const evt of parsed.events) {
        if (evt.event === "chunk") {
          appendTypedText(assistant, evt.data.text || "");
        } else if (evt.event === "meta") {
          const nextTitle = evt.data.title || "";
          if (nextTitle) {
            document.title = nextTitle;
            if (titleEl) titleEl.textContent = nextTitle;
          }
          if (evt.data.session_id) updateSessionId(evt.data.session_id);
          if (evt.data.model_provider) setProviderSelection(evt.data.model_provider);
          if (evt.data.model_provider_label) updateProviderBadge(evt.data.model_provider_label);
        } else if (evt.event === "error") {
          setAssistantError(assistant, evt.data.message || "服务异常，请稍后重试。");
        } else if (evt.event === "done") {
          if (evt.data.session_id) updateSessionId(evt.data.session_id);
          if (evt.data.model_provider) setProviderSelection(evt.data.model_provider);
          if (evt.data.model_provider_label) updateProviderBadge(evt.data.model_provider_label);
          finishAssistant(assistant, evt.data.answer || "已处理完成。");
        }
      }
    }
    if (assistant.classList.contains("streaming")) {
      finishAssistant(assistant, "已处理完成。");
    }
  } catch (_) {
    setAssistantError(assistant, "网络异常，请稍后重试。");
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

function createRipple(target, event) {
  const rect = target.getBoundingClientRect();
  const ripple = document.createElement("span");
  ripple.className = "ripple";
  ripple.style.left = `${event.clientX - rect.left}px`;
  ripple.style.top = `${event.clientY - rect.top}px`;
  target.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  autoResize();
  updateInputState();
  ask(text);
});

inputEl.addEventListener("input", () => {
  autoResize();
  updateInputState();
});

inputEl.addEventListener("focus", updateInputState);
inputEl.addEventListener("blur", updateInputState);

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

if (providerEl) {
  providerEl.addEventListener("change", () => {
    if (!providerEl.value) return;
    localStorage.setItem(MODEL_PROVIDER_KEY, providerEl.value);
    const currentOption = currentProviderOption();
    updateProviderBadge(currentOption ? currentOption.textContent : "");
    refreshModelSelect();
  });
}

modelTriggerEl?.addEventListener("click", toggleModelMenu);

document.addEventListener("click", (event) => {
  if (!modelSelectEl?.contains(event.target)) closeModelMenu();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModelMenu();
});

for (const btn of quickButtons) {
  btn.addEventListener("click", (event) => {
    createRipple(btn, event);
    const q = btn.getAttribute("data-q");
    if (q) ask(q);
  });
}

sendBtn.addEventListener("click", (event) => {
  createRipple(sendBtn, event);
});

async function init() {
  updateInputState();
  showSkeletonScreen();
  getSessionId();
  await loadProviders();
  window.setTimeout(() => {
    hideSkeletonScreen();
    if (!messagesEl.querySelector(".msg")) {
      addMessage("assistant", "你好，我是 WX Agent。你可以直接输入问题，我会基于知识库实时回复。");
    }
  }, 420);
}

init();
