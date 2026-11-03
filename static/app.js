// NetBird 更新台 — 前端。无框架、无构建，只做一件事：把 /api 的状态渲染出来。
const $ = (sel) => document.querySelector(sel);

const state = {
  hosts: [],
  keys: [],
  jobs: [],
  active: null,
  editing: null,
  stream: null,
  lastSeq: 0,
  activeHostIds: new Set(),
};

// ---------- 小工具 ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) throw new Error((data && data.error) || `请求失败 ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(message, kind = "info") {
  let node = $("#toast");
  if (!node) {
    node = el("div", { class: "toast", id: "toast" });
    document.body.append(node);
  }
  node.textContent = message;
  node.dataset.kind = kind;
  node.dataset.show = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => delete node.dataset.show, 4200);
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- 渲染 ----------

function render() {
  renderTopbar();
  renderHosts();
  renderKeys();
  renderHistory();
  renderJobPanel();
}

function renderTopbar() {
  const count = state.hosts.length;
  const last = state.jobs.find((j) => j.status !== "running");
  $("#brand-sub").textContent =
    count === 0 ? "还没有机器" : `${count} 台机器${last ? ` · 上次任务 ${fmtDate(last.created_at)}` : ""}`;
  $("#btn-update-all").disabled = !!state.active || count === 0;
  $("#btn-check-all").disabled = !!state.active || count === 0;
}

function hostState(host) {
  if (state.activeHostIds.has(host.id)) return "running";
  if (host.check && host.check.error) return "failed";
  if (host.check && host.check.reachable) return "current";
  return "idle";
}

function renderHosts() {
  const list = $("#host-list");
  list.textContent = "";
  $("#host-count").textContent = state.hosts.length;
  $("#hosts-empty").hidden = state.hosts.length > 0;

  for (const host of state.hosts) {
    const chips = [
      el("span", { class: "chip chip-muted", text: host.group_name === "console" ? "控制台" : "客户端" }),
    ];
    if (host.detach) chips.push(el("span", { class: "chip chip-warning" }, el("i", { class: "chip-dot" }), "子网代理"));
    if (host.self_host) chips.push(el("span", { class: "chip chip-muted", text: "本机" }));

    const st = hostState(host);
    if (st === "running") {
      chips.push(el("span", { class: "chip chip-live chip-primary" }, el("i", { class: "chip-dot" }), "更新中"));
    } else if (st === "failed") {
      chips.push(el("span", { class: "chip chip-danger" }, el("i", { class: "chip-dot" }), "连不上"));
    } else if (host.check && host.check.reachable) {
      chips.push(el("span", { class: "chip chip-success" }, el("i", { class: "chip-dot" }), "已连通"));
    }

    const meta = [
      el("span", { class: "addr", "data-kind": host.addr.kind }, el("i", { class: "addr-dot" }), host.address),
      el("span", { text: `${host.scripts.length} 个脚本` }),
    ];
    if (host.addr.warn) meta.push(el("span", { class: "err-text", text: host.addr.warn }));
    else if (host.note) meta.push(el("span", { text: host.note }));

    const version = el("span", { class: "version" });
    if (host.check && host.check.version) version.textContent = host.check.version;
    else if (host.check && host.check.error) version.textContent = "—";
    else if (host.check && host.check.reachable) version.textContent = "未安装";
    else version.textContent = "未体检";

    const row = el(
      "li",
      { class: "host", "data-state": st },
      el("span", { class: "host-rail" }),
      el(
        "div",
        { class: "host-main" },
        el(
          "div",
          { class: "host-title" },
          el("span", { class: "host-name", text: host.name, title: "点击编辑", style: "cursor:pointer" }),
          chips,
        ),
        el("div", { class: "host-meta" }, meta),
      ),
      el(
        "div",
        { class: "host-right" },
        version,
        el("button", {
          class: "btn btn-ghost btn-sm",
          text: "更新",
          disabled: !!state.active,
          onclick: (e) => {
            e.stopPropagation();
            confirmUpdate([host]);
          },
        }),
      ),
    );
    row.querySelector(".host-name").addEventListener("click", () => openDrawer(host));
    list.append(row);
  }
}

function renderKeys() {
  const list = $("#key-list");
  list.textContent = "";
  $("#key-count").textContent = state.keys.length;
  $("#keys-empty").hidden = state.keys.length > 0;
  for (const key of state.keys) {
    list.append(
      el(
        "li",
        { class: "key-row" },
        el("span", { class: "key-name", text: key.name }),
        el("span", { class: "key-pub", text: key.public_key || "(无法导出公钥)", title: key.public_key || "" }),
        el("button", { class: "btn btn-ghost btn-sm", text: "删除", onclick: () => deleteKey(key.name) }),
      ),
    );
  }
}

function jobTone(status) {
  return (
    {
      success: ["chip-success", "成功"],
      failed: ["chip-danger", "失败"],
      aborted: ["chip-warning", "已中止"],
      running: ["chip-live chip-primary", "运行中"],
    }[status] || ["chip-muted", status || "空闲"]
  );
}

function renderHistory() {
  const list = $("#run-list");
  list.textContent = "";
  if (!state.jobs.length) {
    list.append(el("li", { class: "run" }, el("span", { class: "run-main muted", text: "还没有任务记录" })));
    return;
  }
  for (const job of state.jobs) {
    const tone = jobTone(job.status);
    const total = (job.spec.targets || []).length;
    const okCount = job.result ? (job.result.hosts || []).filter((h) => h.status === "ok").length : 0;
    const summary = job.status === "running" ? `${total} 台` : `${okCount}/${total} 成功`;
    list.append(
      el(
        "li",
        { class: "run" },
        el("span", {
          class: "run-dot",
          "data-tone": job.status === "success" ? "ok" : job.status === "running" ? "run" : "warn",
        }),
        el(
          "div",
          { class: "run-main" },
          el(
            "div",
            { class: "run-title", text: job.spec.label || "更新任务" },
            el("span", { class: "run-sub", text: ` ${summary}` }),
          ),
          el("div", { class: "run-meta", text: `${fmtDate(job.created_at)} · ${tone[1]}` }),
        ),
        el("span", { class: `chip ${tone[0]}` }, el("i", { class: "chip-dot" }), tone[1]),
        el("button", { class: "btn btn-ghost btn-sm", text: "日志", onclick: () => loadLog(job.id) }),
      ),
    );
  }
}

function renderLogPlaceholder() {
  const log = $("#log");
  if (log.children.length) return;
  log.append(
    el(
      "li",
      { style: "grid-template-columns:1fr" },
      el("span", { class: "dim", text: "还没有任务。点「全部更新」或某台机器的「更新」，日志会实时出现在这里。" }),
    ),
  );
}

function renderJobPanel() {
  renderLogPlaceholder();
  const status = $("#job-status");
  if (state.active) {
    status.className = "chip chip-live chip-primary";
    status.innerHTML = '<i class="chip-dot"></i>运行中';
    $("#btn-abort").disabled = false;
    $("#job-foot-note").textContent = `${state.active.spec.targets.length} 台 · 日志写到 data/jobs/`;
  } else {
    const last = state.jobs[0];
    if (last) {
      const tone = jobTone(last.status);
      status.className = `chip ${tone[0]}`;
      status.innerHTML = `<i class="chip-dot"></i>${tone[1]}`;
    } else {
      status.className = "chip chip-muted";
      status.innerHTML = '<i class="chip-dot"></i>空闲';
    }
    $("#btn-abort").disabled = true;
    $("#job-foot-note").textContent = "日志会写到 data/jobs/";
  }
}

// ---------- 日志流 ----------

function appendLine(entry) {
  const log = $("#log");
  const cls = entry.kind === "ok" ? "ok" : entry.kind === "err" ? "err" : entry.kind === "dim" ? "dim" : "";
  const text = el("span", { class: cls, text: entry.text });
  const cell = el("span");
  if (entry.host) cell.append(el("span", { class: "host-tag", text: entry.host + " " }), text);
  else cell.append(text);
  log.append(el("li", {}, el("span", { class: "t", text: entry.t }), cell));
  log.scrollTop = log.scrollHeight;

  const m = /────\s*(\d+)\/(\d+)\s+(.+?)\s*────/.exec(entry.text);
  if (m) {
    const [, idx, total, name] = m;
    $("#job-progress").hidden = false;
    $("#job-progress-text").textContent = `${idx} / ${total} 台 · 正在处理 ${name}`;
    $("#job-progress-fill").style.width = `${((Number(idx) - 1) / Number(total)) * 100}%`;
    const host = state.hosts.find((h) => h.name === name);
    if (host) {
      state.activeHostIds.add(host.id);
      renderHosts();
    }
  }
}

function attachJob(jobId, from = 0) {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  state.lastSeq = from;
  const src = new EventSource(`/api/jobs/${jobId}/events?from=${from}`);
  state.stream = src;

  src.onmessage = (event) => {
    const entry = JSON.parse(event.data);
    if (entry.seq <= state.lastSeq) return;
    state.lastSeq = entry.seq;
    appendLine(entry);
  };

  src.addEventListener("status", (event) => {
    const data = JSON.parse(event.data);
    if (data.status !== "running") {
      src.close();
      state.stream = null;
      state.active = null;
      state.activeHostIds.clear();
      renderJobPanel();
      refresh().then(() => {
        if (data.status === "success") toast("任务完成", "info");
        else if (data.status === "aborted") toast("任务已中止", "err");
        else toast("任务结束，有机器失败，看日志", "err");
      });
    }
  });
}

async function loadLog(jobId) {
  $("#log").textContent = "";
  $("#job-progress").hidden = true;
  const text = await (await fetch(`/api/jobs/${jobId}/log`)).text();
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const m = /^\[(\d{2}:\d{2}:\d{2})\]\s*(?:\[([^\]]+)\]\s*)?(.*)$/.exec(line);
    if (m) appendLine({ t: m[1], host: m[2] || "", text: m[3], kind: "info" });
  }
  $("#job-foot-note").textContent = "这是历史日志";
}

// ---------- 更新流程 ----------

function optionGroups(hosts) {
  const groups = [];
  const seen = new Set();
  for (const host of hosts) {
    for (const script of host.scripts) {
      if (!script.options.length || seen.has(script.id)) continue;
      seen.add(script.id);
      groups.push({ host, script });
    }
  }
  return groups;
}

function confirmUpdate(hosts) {
  if (!hosts.length) return;
  const ordered = [...hosts];
  const groups = optionGroups(ordered);

  const body = el("div", { style: "display:flex;flex-direction:column;gap:16px" });
  body.append(
    el(
      "div",
      {},
      el("div", { class: "opt-label", text: "执行顺序" }),
      el(
        "ul",
        { class: "target-list" },
        ordered.map((h, i) =>
          el(
            "li",
            {},
            el("span", { class: "idx", text: String(i + 1) }),
            el("span", { text: h.name }),
            h.detach ? el("span", { class: "chip chip-warning" }, "后台执行") : null,
            h.self_host ? el("span", { class: "chip chip-muted" }, "最后执行") : null,
          ),
        ),
      ),
    ),
  );

  const inputs = [];
  for (const { host, script } of groups) {
    const grid = el("div", { class: "opt-grid" });
    const boxes = [];
    for (const opt of script.options) {
      const box = el("input", { type: "checkbox", value: opt, checked: true });
      boxes.push(box);
      grid.append(el("label", { class: "opt" }, box, opt));
    }
    inputs.push({ host, script, boxes });
    body.append(el("div", {}, el("div", { class: "opt-label", text: `${host.name} · ${script.label}` }), grid));
  }

  openModal({
    title: "确认更新",
    desc: `${ordered.length} 台机器 · 控制台分组优先，失败即中止，其后逐台执行`,
    body,
    okText: "开始更新",
    onSubmit: async () => {
      const targets = ordered.map((host) => {
        const args = {};
        for (const item of inputs.filter((i) => i.host.id === host.id)) {
          const chosen = item.boxes.filter((b) => b.checked).map((b) => b.value);
          if (chosen.length !== item.script.options.length) args[item.script.id] = chosen;
        }
        return Object.keys(args).length ? { host_id: host.id, args } : { host_id: host.id };
      });
      const job = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          targets,
          label: ordered.length > 1 ? "全量更新" : `更新 ${ordered[0].name}`,
        }),
      });
      state.active = job;
      state.activeHostIds = new Set(ordered.map((h) => h.id));
      $("#log").textContent = "";
      $("#job-progress").hidden = false;
      $("#job-progress-text").textContent = "准备中…";
      $("#job-progress-fill").style.width = "0%";
      renderJobPanel();
      renderHosts();
      attachJob(job.id, 0);
    },
  });
}


// ---------- 脚本行编辑器 ----------

function scriptField(text, hint, input, wide = false) {
  return el(
    "label",
    { class: `script-field${wide ? " script-field-wide" : ""}` },
    el("span", { class: "script-label" }, text, hint ? el("span", { class: "script-label-hint", text: hint }) : null),
    input,
  );
}

function scriptRow(data = {}) {
  const row = el(
    "div",
    { class: "script-row" },
    scriptField(
      "脚本路径",
      "目标机上已存在，不会上传",
      el("input", {
        class: "input mono",
        "data-f": "path",
        placeholder: "/root/netbird/update-client.sh",
        value: data.path || "",
        spellcheck: "false",
      }),
      true,
    ),
    scriptField(
      "显示名",
      "可留空",
      el("input", { class: "input", "data-f": "label", placeholder: "更新客户端", value: data.label || "" }),
    ),
    scriptField(
      "组件",
      "可选",
      el("input", {
        class: "input mono",
        "data-f": "options",
        placeholder: "management,dashboard",
        value: (data.options || []).join(","),
        spellcheck: "false",
      }),
    ),
    el(
      "div",
      { class: "script-tools" },
      el("button", { class: "btn", "data-act": "up", type: "button", title: "上移", text: "↑" }),
      el("button", { class: "btn", "data-act": "down", type: "button", title: "下移", text: "↓" }),
      el("button", { class: "btn", "data-act": "del", type: "button", title: "删除", text: "×" }),
    ),
  );
  return row;
}

function renderScriptRows(scripts) {
  const box = $("#script-rows");
  box.textContent = "";
  const list = scripts && scripts.length ? scripts : [{}];
  for (const script of list) box.append(scriptRow(script));
  refreshScriptTools();
}

function refreshScriptTools() {
  const rows = [...document.querySelectorAll("#script-rows .script-row")];
  rows.forEach((row, i) => {
    row.querySelector('[data-act="up"]').disabled = i === 0;
    row.querySelector('[data-act="down"]').disabled = i === rows.length - 1;
    row.querySelector('[data-act="del"]').disabled = rows.length === 1;
  });
}

function collectScripts() {
  const rows = [...document.querySelectorAll("#script-rows .script-row")];
  const lines = [];
  for (const row of rows) {
    const get = (f) => row.querySelector(`[data-f="${f}"]`).value.trim();
    const path = get("path");
    if (!path) continue;
    lines.push(`${path} | ${get("label")} | ${get("options")}`);
  }
  return lines.join("\n");
}

// ---------- 抽屉 ----------

function fillKeys(selected) {
  const select = $("#f-key");
  select.textContent = "";
  for (const key of state.keys) {
    select.append(el("option", { value: key.name, text: key.name, selected: key.name === selected }));
  }
  // 留一个「不用密钥」的口子：只适合在容器外跑、且依赖 ssh-agent / ~/.ssh 默认身份的场景
  select.append(
    el("option", {
      value: "",
      text: state.keys.length ? "不使用密钥（依赖默认 SSH 身份，容器里通常没有）" : "还没有密钥，先到下面「SSH 密钥」添加",
      selected: !selected,
    }),
  );
}

function updateBootstrap() {
  const keyName = $("#f-key").value;
  const user = $("#f-user").value || "root";
  const wrap = $("#bootstrap-wrap");
  const key = state.keys.find((k) => k.name === keyName);
  if (!key || !key.public_key) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  $("#bootstrap-title").textContent = `在 ${$("#f-addr").value || "目标机"} 上以 ${user} 身份执行`;
  $("#bootstrap-cmd").textContent = [
    "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
    `echo '${key.public_key}' >> ~/.ssh/authorized_keys`,
    "chmod 600 ~/.ssh/authorized_keys",
  ].join("\n");
}

function openDrawer(host = null) {
  state.editing = host;
  $("#drawer-title").textContent = host ? `编辑 ${host.name}` : "添加机器";
  $("#drawer-sub").textContent = host
    ? "改完保存即可生效；脚本顺序就是执行顺序"
    : "先填连接信息，用下面的命令装公钥，再点「测试连接」";
  $("#f-name").value = host?.name || "";
  $("#f-addr").value = host?.address || "";
  $("#f-port").value = host?.ssh_port || 22;
  $("#f-user").value = host?.ssh_user || "root";
  $("#f-group").value = host?.group_name || "clients";
  renderScriptRows(host ? host.scripts : []);
  $("#f-detach").checked = !!host?.detach;
  $("#f-detach-log").value = host?.detach_log || "/var/log/netbird-upgrade.log";
  $("#f-self").checked = !!host?.self_host;
  $("#f-test-result").textContent = host?.check?.reachable
    ? `上次体检：netbird ${host.check.version || "未安装"}`
    : "还没测试";
  $("#f-test-result").className = "hint";
  $("#btn-delete").hidden = !host;
  fillKeys(host?.key_name || (state.keys[0] && state.keys[0].name));
  $("#detach-extra").hidden = !$("#f-detach").checked;
  updateBootstrap();
  document.body.dataset.drawer = "open";
  $("#drawer").setAttribute("aria-hidden", "false");
  setTimeout(() => $("#f-name").focus(), 60);
}

function closeDrawer() {
  delete document.body.dataset.drawer;
  $("#drawer").setAttribute("aria-hidden", "true");
  state.editing = null;
}

function formPayload() {
  return {
    name: $("#f-name").value.trim(),
    address: $("#f-addr").value.trim(),
    ssh_port: Number($("#f-port").value) || 22,
    ssh_user: $("#f-user").value.trim() || "root",
    group_name: $("#f-group").value,
    key_name: $("#f-key").value,
    scripts: collectScripts(),
    detach: $("#f-detach").checked,
    detach_log: $("#f-detach-log").value.trim() || "/var/log/netbird-upgrade.log",
    self_host: $("#f-self").checked,
  };
}

async function saveHost() {
  try {
    const payload = formPayload();
    if (state.editing) await api(`/api/hosts/${state.editing.id}`, { method: "PUT", body: JSON.stringify(payload) });
    else await api("/api/hosts", { method: "POST", body: JSON.stringify(payload) });
    closeDrawer();
    await refresh();
    toast("已保存", "info");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function deleteHost() {
  if (!state.editing) return;
  if (!confirm(`删除 ${state.editing.name}？只会从清单里移除，不动目标机。`)) return;
  try {
    await api(`/api/hosts/${state.editing.id}`, { method: "DELETE" });
    closeDrawer();
    await refresh();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function testConnection() {
  const box = $("#f-test-result");
  box.className = "hint";
  box.textContent = "测试中…";
  try {
    const payload = formPayload();
    const result = await api("/api/hosts/test", { method: "POST", body: JSON.stringify(payload) });
    if (result.ok) {
      box.className = "hint";
      box.textContent = `✓ 连通 · ${result.addr.label} · netbird ${result.version || "未安装"}`;
    } else {
      box.className = "hint err-text";
      box.textContent = `✗ ${result.error || "连不上"}`;
    }
  } catch (err) {
    box.className = "hint err-text";
    box.textContent = `✗ ${err.message}`;
  }
}

// ---------- 密钥 ----------

function openKeyModal() {
  const name = el("input", { class: "input", id: "k-name", placeholder: "nbtool-ed25519", value: "nbtool-ed25519" });
  const textarea = el("textarea", { class: "textarea", placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----" });
  const generate = el("input", { type: "checkbox", checked: true });
  const keyField = el("div", { class: "field", hidden: true }, el("label", { text: "私钥内容" }), textarea);
  generate.addEventListener("change", () => {
    keyField.hidden = generate.checked;
  });

  const body = el(
    "div",
    { style: "display:flex;flex-direction:column;gap:16px" },
    el("div", { class: "field" }, el("label", { text: "名称" }), name),
    el("label", { class: "opt" }, generate, "让服务端生成一把 ed25519 密钥"),
    keyField,
    el("p", { class: "hint", text: "粘贴已有私钥时不能带密码短语（ssh 在 BatchMode 下无法输入密码）。" }),
  );

  openModal({
    title: "添加密钥",
    desc: "一把密钥可以给所有机器共用；每台机器上装的是同一把公钥，不需要逐台生成",
    body,
    okText: "创建",
    onSubmit: async () => {
      const payload = generate.checked
        ? { name: name.value.trim(), generate: true }
        : { name: name.value.trim(), private_key: textarea.value };
      const key = await api("/api/keys", { method: "POST", body: JSON.stringify(payload) });
      await refresh();
      toast(`已创建密钥 ${key.name}`, "info");
    },
  });
}

async function deleteKey(name) {
  if (!confirm(`删除密钥 ${name}？`)) return;
  try {
    await api(`/api/keys/${encodeURIComponent(name)}`, { method: "DELETE" });
    await refresh();
  } catch (err) {
    toast(err.message, "err");
  }
}

// ---------- 模态 ----------

let modalSubmit = null;

function openModal({ title, desc, body, okText = "确认", onSubmit }) {
  $("#modal-title").textContent = title;
  $("#modal-desc").textContent = desc || "";
  const slot = $("#modal-body");
  slot.textContent = "";
  slot.append(body);
  $("#modal-ok").textContent = okText;
  modalSubmit = onSubmit;
  document.body.dataset.modal = "open";
}

function closeModal() {
  delete document.body.dataset.modal;
  modalSubmit = null;
}

async function submitModal() {
  if (!modalSubmit) {
    closeModal();
    return;
  }
  const fn = modalSubmit;
  $("#modal-ok").disabled = true;
  try {
    await fn();
    closeModal();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    $("#modal-ok").disabled = false;
  }
}

// ---------- 数据加载 ----------

async function refresh() {
  const data = await api("/api/state");
  state.hosts = data.hosts;
  state.keys = data.keys;
  state.jobs = data.jobs;
  state.active = data.active;
  if (data.active && !state.stream) {
    state.activeHostIds = new Set((data.active.spec.targets || []).map((t) => t.host_id));
    $("#log").textContent = "";
    $("#job-progress").hidden = false;
    attachJob(data.active.id, 0);
  }
  render();
}

async function checkAll() {
  toast("开始体检…", "info");
  for (const host of state.hosts) {
    try {
      const result = await api(`/api/hosts/${host.id}/check`, { method: "POST" });
      Object.assign(host, result.host);
    } catch (err) {
      host.check = { reachable: false, error: err.message };
    }
    renderHosts();
  }
  toast("体检完成", "info");
}

// ---------- 绑定 ----------

function bind() {
  $("#btn-add-host").addEventListener("click", () => openDrawer());
  $("#btn-empty-add").addEventListener("click", () => openDrawer());
  $("#btn-close-drawer").addEventListener("click", closeDrawer);
  $("#btn-cancel").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  $("#btn-save").addEventListener("click", saveHost);
  $("#btn-delete").addEventListener("click", deleteHost);
  $("#btn-test").addEventListener("click", testConnection);
  $("#f-key").addEventListener("change", updateBootstrap);
  $("#f-addr").addEventListener("input", updateBootstrap);
  $("#f-user").addEventListener("input", updateBootstrap);
  $("#btn-add-script").addEventListener("click", () => {
    $("#script-rows").append(scriptRow({}));
    refreshScriptTools();
    $("#script-rows").lastElementChild.querySelector('[data-f="path"]').focus();
  });

  $("#script-rows").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const row = button.closest(".script-row");
    const rows = [...document.querySelectorAll("#script-rows .script-row")];
    const act = button.dataset.act;
    if (act === "del") row.remove();
    if (act === "up" && row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    if (act === "down" && row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
    if (!rows.length) $("#script-rows").append(scriptRow({}));
    refreshScriptTools();
  });

  $("#f-detach").addEventListener("change", () => {
    $("#detach-extra").hidden = !$("#f-detach").checked;
  });
  $("#btn-copy-bootstrap").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#bootstrap-cmd").textContent);
      toast("已复制", "info");
    } catch {
      toast("复制失败，请手动选中", "err");
    }
  });

  $("#btn-add-key").addEventListener("click", openKeyModal);
  $("#modal-cancel").addEventListener("click", closeModal);
  $("#modal-scrim").addEventListener("click", closeModal);
  $("#modal-ok").addEventListener("click", submitModal);

  $("#btn-update-all").addEventListener("click", () => confirmUpdate(state.hosts));
  $("#btn-check-all").addEventListener("click", checkAll);
  $("#btn-abort").addEventListener("click", async () => {
    if (!state.active) return;
    if (!confirm("中止任务？正在跑的脚本会被杀掉。")) return;
    await api(`/api/jobs/${state.active.id}/abort`, { method: "POST" });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeDrawer();
    closeModal();
  });
}

// 直达：index.html#add 直接打开抽屉（方便截图和书签）
if (location.hash === "#add") setTimeout(() => openDrawer(), 300);

bind();
refresh().catch((err) => toast(err.message, "err"));
setInterval(() => {
  if (!document.hidden && !state.stream) refresh().catch(() => {});
}, 15000);
