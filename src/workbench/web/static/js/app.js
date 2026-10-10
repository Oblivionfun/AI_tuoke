// 冠维获客工作台的前端交互。服务端渲染为主，这里只做：提示消息、快捷键、复制、抽屉、运行页实时步骤。
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // ---------- 提示消息：3-5 秒后自动消失 ----------

  function toast({ text, error = false, link = "", linkText = "" }) {
    const box = $("#toasts");
    if (!box || !text) return;
    const el = document.createElement("div");
    el.className = "toast" + (error ? " error" : "");
    el.textContent = text;
    if (link) {
      const a = document.createElement("a");
      a.href = link;
      a.textContent = linkText || "查看";
      el.appendChild(a);
    }
    box.appendChild(el);
    setTimeout(() => el.remove(), error ? 5000 : 4000);
  }

  function showFlashes() {
    $$("template[data-flash]").forEach((t) => {
      toast({ text: t.innerHTML.trim(), error: t.dataset.error === "true" });
      t.remove();
    });
  }

  document.body.addEventListener("toast", (event) => toast(event.detail || {}));

  // ---------- 复制 ----------

  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      toast({ text: "已复制" });
    } catch {
      toast({ text: "复制失败，请手动选择文字", error: true });
    }
    button?.blur();
  }

  // ---------- 点击行为（事件委托，htmx 换入的内容也生效） ----------

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-copy], [data-copy-subject], [data-reveal], [data-close], [data-fill], [data-dialog-open], [data-dialog-close]");
    if (target) {
      if (target.dataset.copy !== undefined) copy(target.dataset.copy, target);
      if (target.dataset.copySubject) {
        const subject = $("#" + target.dataset.copySubject)?.value || "";
        const body = $("#" + target.dataset.copyBody)?.value || "";
        copy(`Subject: ${subject}\n\n${body}`, target);
      }
      if (target.dataset.reveal) reveal(target.dataset.reveal);
      if (target.dataset.close !== undefined) target.closest(".reason-form")?.classList.remove("open");
      if (target.dataset.fill) {
        const input = $("#" + target.dataset.fill);
        if (input) { input.value = target.textContent.trim(); input.focus(); }
      }
      if (target.dataset.dialogOpen) $("#" + target.dataset.dialogOpen)?.showModal();
      if (target.dataset.dialogClose !== undefined) target.closest("dialog")?.close();
      return;
    }
    // 整行可点：点在行内的链接、按钮、输入框上时不跳转
    const row = event.target.closest("tr[data-href]");
    if (row && !event.target.closest("a, button, input, select, textarea, label")) {
      if (event.metaKey || event.ctrlKey) window.open(row.dataset.href, "_blank");
      else window.location.href = row.dataset.href;
    }
  });

  // 点击抽屉、弹窗的遮罩区域关闭
  document.addEventListener("click", (event) => {
    if (event.target instanceof HTMLDialogElement) event.target.close();
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("select[data-autosubmit]")) event.target.form.requestSubmit();
  });

  document.addEventListener("submit", (event) => {
    const message = event.submitter?.dataset.confirm || event.target.dataset.confirm;
    if (message && !window.confirm(message)) { event.preventDefault(); return; }
    if (event.target.matches("[data-dirty-guard]")) dirty.delete(event.target);
  });

  // 草稿改了没保存就离开页面时提醒
  const dirty = new Set();
  document.addEventListener("input", (event) => {
    const form = event.target.closest("form[data-dirty-guard]");
    if (form) dirty.add(form);
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty.size) event.preventDefault();
  });

  function reveal(id) {
    $$(".reason-form.open").forEach((f) => f.id !== id && f.classList.remove("open"));
    const form = $("#" + id);
    if (!form) return;
    form.classList.add("open");
    form.querySelector("input:not([type=hidden])")?.focus();
  }

  // ---------- 今日审核快捷键 ----------

  let keyboardSwap = false;

  document.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const typing = event.target.closest("input, textarea, select, [contenteditable]");
    if (event.key === "Escape") {
      $$(".reason-form.open").forEach((f) => f.classList.remove("open"));
      if (typing) event.target.blur();
      return;
    }
    if (typing || !$("#review")) return;
    const key = event.key.toLowerCase();
    if (key === "?") { $("#keys-dialog")?.showModal(); event.preventDefault(); return; }
    if (key === "j" || key === "k") {
      const items = $$("[data-queue-item]");
      const index = items.findIndex((el) => el.getAttribute("aria-current") === "true");
      const next = items[Math.min(Math.max(index + (key === "j" ? 1 : -1), 0), items.length - 1)];
      if (next && next.getAttribute("aria-current") !== "true") { keyboardSwap = true; next.click(); }
      event.preventDefault();
      return;
    }
    const button = $(`[data-key="${key}"]`);
    if (button) { keyboardSwap = true; button.click(); event.preventDefault(); }
  });

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.target?.id === "review") $("#decision")?.setAttribute("aria-busy", "true");
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    if (target?.id === "drawer-body") target.querySelector("input")?.focus();
    if ($("#review") && keyboardSwap) {
      keyboardSwap = false;
      const title = $("#card-title");
      if (title) { title.setAttribute("tabindex", "-1"); title.focus({ preventScroll: true }); }
    }
  });

  document.body.addEventListener("htmx:responseError", () => toast({ text: "操作失败，请刷新页面后重试", error: true }));

  // ---------- 运行页：SSE 实时步骤 ----------

  const ICON_PATHS = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    open: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    review: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    status: '<polyline points="20 6 9 17 4 12"/>',
  };
  const DOT = '<circle cx="12" cy="12" r="2.5"/>';

  function stepItem(step) {
    const li = document.createElement("li");
    li.className = "k-" + step.kind;
    const time = document.createElement("time");
    time.textContent = step.at;
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "icon");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = ICON_PATHS[step.kind] || DOT;
    const text = document.createElement("span");
    text.textContent = step.text;
    li.append(time, icon, text);
    return li;
  }

  function bump(stat) {
    const el = $(`[data-stat="${stat}"]`);
    if (el) el.textContent = String((parseInt(el.textContent, 10) || 0) + 1);
  }

  function followRun() {
    const marker = $("[data-run-stream]");
    if (!marker || !window.EventSource) return;
    const source = new EventSource(marker.dataset.runStream);
    source.addEventListener("step", (message) => {
      const step = JSON.parse(message.data);
      $("#waiting")?.remove();
      const list = $(`[data-steps="${step.run}"]`) || $(`[data-steps="${marker.dataset.rootRun}"]`);
      if (!list) return;
      list.querySelector("li.muted")?.remove();
      list.appendChild(stepItem(step));
      const lane = list.closest("details");
      if (lane && !lane.open) lane.open = true;
      if (step.kind === "search") bump("searches");
      if (step.kind === "open") bump("opened");
      if (step.kind === "status") bump("done");
    });
    source.addEventListener("done", () => { source.close(); setTimeout(() => window.location.reload(), 800); });
  }

  // 脚本以 defer 加载，执行时页面已解析完
  showFlashes();
  followRun();
})();
