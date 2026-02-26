const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const quickButtons = document.querySelectorAll(".quick");
const titleEl = document.getElementById("chatTitle");

let busy = false;

function addMessage(role, text, streaming = false) {
  const div = document.createElement("div");
  div.className = `msg ${role}${streaming ? " streaming" : ""}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
  return div;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 160)}px`;
}

function setBusy(nextBusy) {
  busy = nextBusy;
  sendBtn.disabled = nextBusy;
  inputEl.disabled = nextBusy;
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
    if (data) {
      try {
        events.push({ event, data: JSON.parse(data) });
      } catch (_) {
        events.push({ event, data: { text: data } });
      }
    }
  }
  return { events, rest };
}

async function ask(message) {
  if (!message || busy) return;
  setBusy(true);
  addMessage("user", message);
  const assistant = addMessage("assistant", "", true);

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });

    if (!resp.ok || !resp.body) {
      assistant.textContent = "请求失败，请稍后重试。";
      assistant.classList.remove("streaming");
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
          assistant.textContent += evt.data.text || "";
          scrollToBottom();
        } else if (evt.event === "meta") {
          const nextTitle = evt.data.title || "";
          if (nextTitle) {
            document.title = nextTitle;
            if (titleEl) titleEl.textContent = nextTitle;
          }
        } else if (evt.event === "error") {
          assistant.textContent = evt.data.message || "服务异常，请稍后重试。";
        } else if (evt.event === "done") {
          if (!assistant.textContent.trim()) {
            assistant.textContent = evt.data.answer || "已处理完成。";
          }
          assistant.classList.remove("streaming");
        }
      }
    }
  } catch (_) {
    assistant.textContent = "网络异常，请稍后重试。";
    assistant.classList.remove("streaming");
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  autoResize();
  ask(text);
});

inputEl.addEventListener("input", autoResize);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

for (const btn of quickButtons) {
  btn.addEventListener("click", () => {
    const q = btn.getAttribute("data-q");
    if (q) ask(q);
  });
}

addMessage(
  "assistant",
  "你好，我是 WX Agent。你可以直接输入问题，我会基于知识库实时回复。"
);
