/**
 * 原生前端逻辑（无任何构建步骤）。
 *
 * 两个关键取舍：
 *
 * 1. **没有使用 EventSource**。EventSource 只支持 GET，而发起一轮对话需要携带
 *    可能很长的 JSON 请求体，因此改用 fetch + ReadableStream 手动解析 SSE 帧。
 *
 * 2. **会话由 URL 承载**（`#/c/{thread_id}`），且「草稿态」不申请会话。
 *    会话的诞生时刻是「首条消息被接受」，所以打开页面、刷新页面、点「新会话」
 *    都不会在服务端留下任何记录；只有真正发出第一条消息时才会申请 ID 并写进 URL。
 *    这样刷新天然是「恢复」而不是「重置」。
 */
(function () {
  'use strict';

  /** 历史消息中工具输出的展示上限，避免一次刷新就灌入数十万字符。 */
  const HISTORY_PREVIEW_LIMIT = 2000;

  /** 会话清单一次拉取的条数上限（后端硬上限为 200）。 */
  const THREAD_PAGE_SIZE = 50;

  /** 与后端 uuid4().hex 一致的会话 ID 形状，用于校验 URL 片段。 */
  const THREAD_HASH_PATTERN = /^#\/c\/([A-Za-z0-9_-]{1,128})$/;

  const els = {
    messages: document.getElementById('messages'),
    input: document.getElementById('input'),
    send: document.getElementById('send'),
    stop: document.getElementById('stop'),
    newThread: document.getElementById('new-thread'),
    modelSelect: document.getElementById('model-select'),
    threadList: document.getElementById('thread-list'),
    threadSearch: document.getElementById('thread-search'),
    threadArchived: document.getElementById('thread-archived'),
    authStatus: document.getElementById('auth-status'),
    adminModal: document.getElementById('admin-modal'),
    adminClose: document.getElementById('admin-close'),
    adminTabs: document.querySelectorAll('.modal-tabs .tab'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    apikeyForm: document.getElementById('apikey-form'),
    apikeyList: document.getElementById('apikey-list'),
    apikeyPopup: document.getElementById('apikey-popup'),
    apikeyValue: document.getElementById('apikey-value'),
    apikeyCopy: document.getElementById('apikey-copy'),
    popupClose: document.getElementById('popup-close'),
    copyMsg: document.getElementById('copy-msg'),
    auditRefresh: document.getElementById('audit-refresh'),
    auditTableBody: document.querySelector('#audit-table tbody'),
    memoryOpen: document.getElementById('memory-open'),
    memoryModal: document.getElementById('memory-modal'),
    memoryClose: document.getElementById('memory-close'),
    memoryRefresh: document.getElementById('memory-refresh'),
    memoryList: document.getElementById('memory-list'),
    workspaceOpen: document.getElementById('workspace-open'),
    workspaceModal: document.getElementById('workspace-modal'),
    workspaceClose: document.getElementById('workspace-close'),
    workspaceTree: document.getElementById('workspace-tree'),
    workspacePreview: document.getElementById('workspace-preview'),
  };

  const state = {
    threadId: null,
    running: false,
    assistantEl: null,
    toolNodes: [],
    auth: { mode: 'disabled', principal: null },
    admin: { apikeys: [] },
    /** 会话清单的过滤条件；与界面控件保持一致，刷新清单时统一从这里取。 */
    threadFilter: { query: '', includeArchived: false },
    memories: { items: [], truncated: false },
    /**
     * 工作区目录树状态。
     *
     * WHY 只存「已加载的目录」而不是整棵树：面板按需展开下一层，未展开的目录
     * 不该存在于客户端状态里——否则「树」与「服务端实际情况」就有两份真相。
     */
    workspace: { dirs: {}, expanded: {}, selected: null, text: null },
    /** 编辑态：待改写的用户消息下标；null 表示正常发送新消息。 */
    editTarget: null,
    /** 正在查看的分支标识；null 表示会话的当前分支。 */
    branch: null,
  };

  /* ------------------------------------------------------------------ 工具函数 */

  /**
   * 创建 DOM 节点。
   * WHY 全部走 textContent：工具输出与文件内容可能包含 HTML/脚本，
   * 用 innerHTML 拼接等价于给自己开一个 XSS 入口。
   */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function scrollToBottom() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function setRunning(running) {
    state.running = running;
    els.send.disabled = running;
    els.send.textContent = running ? '运行中' : '发送';
    // 运行中暴露停止按钮：发送被禁用的同一时刻必须能停止，
    // 否则长任务一旦发起就只能干等（或刷新页面断开连接）
    els.stop.hidden = !running;
    els.stop.disabled = false;
  }

  /**
   * 把后端返回的 ISO8601 UTC 时间格式化为列表用的短文本。
   * 当天只显示时分，跨天补上日期；无法解析时原样返回，避免显示成 Invalid Date。
   */
  function formatTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);

    const pad = (n) => String(n).padStart(2, '0');
    const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return clock;
    return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${clock}`;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      // WHY 认证模式下 401 直接跳转登录页：SSE / fetch 的 401 不便展示登录弹窗，
      // 让浏览器走完整 OIDC 授权码流程是最稳的做法；disabled 模式下保持原错误提示。
      if (response.status === 401 && state.auth.mode !== 'disabled') {
        window.location.href = state.auth.login_url || '/auth/login';
        throw new Error('未认证，即将跳转登录页');
      }
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (err) {
        /* 响应体不是 JSON 时保持默认文案 */
      }
      // WHY 带上状态码：同一句 detail 文案可能来自不同语义的失败（例如
      // 「审核已过期」是 409、「未认证」是 401），调用方需要据此决定是
      // 「提示后重试」还是「作废当前 UI 状态」，只有文案不足以判断。
      const error = new Error(detail);
      error.status = response.status;
      // WHY 单独带上 Retry-After：429 的语义是「稍后再来」，而「稍后」是多久只有
      // 响应头知道；让调用方去解析错误文案里的秒数，等于把结构化信息降级成字符串匹配。
      error.retryAfter = response.headers.get('Retry-After');
      throw error;
    }
    return response;
  }

  /* ------------------------------------------------------------------ 会话与 URL */

  /** 从 `#/c/{id}` 解析会话 ID；无则返回 null，表示草稿态。 */
  function readThreadIdFromUrl() {
    const match = THREAD_HASH_PATTERN.exec(location.hash);
    return match ? match[1] : null;
  }

  /**
   * 切换 URL 中的会话标识。
   *
   * WHY 用 location.hash 而不是 history.pushState：赋值 hash 会触发 hashchange，
   * 于是「URL 变化 → 同步界面」只有 syncWithUrl 一个入口，不会出现两处逻辑漂移；
   * 同时刷新与浏览器前进/后退都能正确恢复会话，且静态挂载下无需后端加路由。
   */
  function navigate(hash) {
    if (location.hash === hash) {
      // 值没变不会触发 hashchange，必须手动同步，否则「新会话」按钮看起来无效
      syncWithUrl();
      return;
    }
    location.hash = hash;
  }

  /** 按当前 URL 同步界面：有会话 ID 就恢复历史，没有就回到草稿态。 */
  async function syncWithUrl() {
    const threadId = readThreadIdFromUrl();

    if (!threadId) {
      startDraft();
      return;
    }
    if (threadId === state.threadId) return;

    await openThread(threadId);
  }

  /**
   * 进入草稿态：清空界面，但**不申请、也不登记**任何会话。
   * WHY 不申请：纯浏览的访客对服务端应当零写入；ID 等到首次发送时再要。
   */
  function startDraft() {
    state.threadId = null;
    state.assistantEl = null;
    state.toolNodes = [];
    els.messages.innerHTML = '';
    els.messages.appendChild(
      el('div', 'thread-empty', '新会话：发送第一条消息后才会创建')
    );
    markActiveThread();
    els.input.focus();
  }

  /** 草稿态下首次发送时申请会话 ID（服务端只发号，不落库）。 */
  async function ensureThread() {
    if (state.threadId) return state.threadId;

    const response = await api('/api/threads', { method: 'POST' });
    const payload = await response.json();
    state.threadId = payload.thread_id;
    return state.threadId;
  }

  /* ------------------------------------------------------------------ SSE 解析 */

  /**
   * 解析一帧 SSE 文本。
   * @returns {{event: string, data: object}|null}
   */
  function parseFrame(frame) {
    let name = 'message';
    const dataLines = [];

    frame.split(/\r?\n/).forEach((line) => {
      if (line.startsWith('event:')) {
        name = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim());
      }
    });

    if (!dataLines.length) return null;
    try {
      return { event: name, data: JSON.parse(dataLines.join('\n')) };
    } catch (err) {
      return { event: name, data: { raw: dataLines.join('\n') } };
    }
  }

  /**
   * 读取 SSE 流并逐帧回调。
   * WHY 手动拼 buffer：一次网络读可能截断在多帧中间，只有按空行切 Frame
   * 才能保证 JSON 完整。
   */
  async function consumeSSE(response, onFrame) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let idx;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const parsed = parseFrame(frame);
          if (parsed) onFrame(parsed.event, parsed.data);
        }
      }
    } finally {
      // WHY 必须释放 reader：异常中途退出时若不 cancel，连接会一直挂着
      reader.releaseLock();
    }
  }

  /* ------------------------------------------------------------------ 会话清单 */

  function markActiveThread() {
    Array.from(els.threadList.children).forEach((node) => {
      const id = node.dataset ? node.dataset.threadId : null;
      node.classList.toggle('active', Boolean(id) && id === state.threadId);
    });
  }

  /** 会话条目的行内操作：重命名 / 归档（已归档时是恢复）。 */
  function threadActions(item) {
    const actions = el('div', 't-actions');

    const rename = el('button', 'icon-action', '重命名');
    rename.type = 'button';
    rename.addEventListener('click', (event) => {
      // WHY 阻止冒泡：按钮在整行之内，不拦下这次点击就会连带触发「打开会话」
      event.stopPropagation();
      renameThread(item);
    });
    actions.appendChild(rename);

    const archive = el('button', 'icon-action', item.archived ? '恢复' : '归档');
    archive.type = 'button';
    archive.addEventListener('click', (event) => {
      event.stopPropagation();
      setThreadArchived(item, !item.archived);
    });
    actions.appendChild(archive);

    return actions;
  }

  function renderThreads(items) {
    els.threadList.innerHTML = '';

    if (!items.length) {
      els.threadList.appendChild(el('div', 'thread-empty', '暂无历史会话'));
      return;
    }

    items.forEach((item) => {
      const node = el('div', 'thread-item');
      node.dataset.threadId = item.thread_id;

      const head = el('div', 't-head');
      head.appendChild(el('div', 't-title', item.title || '未命名会话'));
      // 归档条目只有勾了「含已归档」才会出现，必须带标记，否则用户会以为
      // 清单里混进了不该出现的东西
      if (item.archived) head.appendChild(el('span', 't-badge', '已归档'));
      node.appendChild(head);

      const sub = [formatTime(item.updated_at)];
      if (item.turn_count > 0) sub.push(`${item.turn_count} 轮`);
      node.appendChild(el('div', 't-sub', sub.filter(Boolean).join(' · ')));
      node.appendChild(threadActions(item));

      node.addEventListener('click', () => {
        // 运行中禁止切换：事件流绑定在当前会话上，换 ID 会把输出渲染进错误的窗口
        if (state.running) return;
        navigate(`#/c/${item.thread_id}`);
      });
      els.threadList.appendChild(node);
    });

    markActiveThread();
  }

  /** 重命名会话；取消或留空视为放弃。 */
  async function renameThread(item) {
    const next = window.prompt('新标题', item.title || '');
    if (next === null) return;
    const title = next.trim();
    // WHY 留空直接返回而不是提交：空标题会被服务端拒绝，弹一次「标题不能为空」
    // 的报错对用户没有任何帮助——他刚刚只是清空了输入框。
    if (!title) return;

    try {
      await api(`/api/threads/${encodeURIComponent(item.thread_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title }),
      });
      await loadThreads();
    } catch (err) {
      appendError('重命名失败：' + err.message);
    }
  }

  /** 归档或恢复会话。 */
  async function setThreadArchived(item, archived) {
    try {
      await api(`/api/threads/${encodeURIComponent(item.thread_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: archived }),
      });
      await loadThreads();
      // WHY 归档当前会话时不切走界面：归档只是从清单里收起来，历史仍可读；
      // 把消息区一并清空会让人以为「会话被删了」，而用户只是想整理列表。
      if (archived && item.thread_id === state.threadId) {
        appendNotice('该会话已归档：打开「含已归档」可以找回。');
      }
    } catch (err) {
      appendError((archived ? '归档' : '恢复') + '失败：' + err.message);
    }
  }

  async function loadThreads() {
    const filter = state.threadFilter;
    const params = new URLSearchParams({ limit: String(THREAD_PAGE_SIZE) });
    if (filter.query) params.set('query', filter.query);
    if (filter.includeArchived) params.set('include_archived', 'true');

    try {
      const response = await api(`/api/threads?${params.toString()}`);
      const payload = await response.json();
      renderThreads(payload.items || []);
    } catch (err) {
      appendError('会话列表加载失败：' + err.message);
    }
  }

  /** 加载历史会话的消息并重绘。 */
  async function openThread(threadId) {
    if (state.running || !threadId) return;

    state.threadId = threadId;
    state.assistantEl = null;
    state.toolNodes = [];
    state.editTarget = null;
    els.messages.innerHTML = '';
    markActiveThread();

    try {
      const response = await api(historyUrl(threadId));
      const messages = await response.json();
      renderHistory(messages);
      await renderConversationControls();
    } catch (err) {
      appendError('加载会话历史失败：' + err.message);
    }
  }

  /**
   * 历史地址：带上正在查看的分支。
   *
   * WHY 分支走查询参数而不是路径段：根分支的标识是空串，路径上无法表达空值。
   */
  function historyUrl(threadId) {
    const base = `/api/threads/${encodeURIComponent(threadId)}`;
    if (!state.branch) return base;
    return `${base}?branch=${encodeURIComponent(state.branch)}`;
  }

  /* ------------------------------------------------------------------ 渲染 */

  function appendError(message) {
    els.messages.appendChild(el('div', 'msg error', `错误：${message}`));
    scrollToBottom();
  }

  /**
   * 提示一条「不是错误」的状态。
   *
   * WHY 与 appendError 分开：混用会让「稍后重试」看起来像一次失败。用户看到
   * 「错误：服务繁忙」的第一反应是去检查自己哪里做错了，而这里其实是服务端在
   * 说「稍等，我接着办」。
   */
  function appendNotice(message) {
    els.messages.appendChild(el('div', 'msg notice', message));
    scrollToBottom();
  }

  /**
   * 发起一次流式运行；服务端说「稍后再来」时自动重试一次。
   *
   * WHY 自动重试而不是把 429 抛给用户：429 的含义是「现在忙，稍后可重试」——把它
   * 显示成一个错误，等于让用户为服务端的容量状况手动补一次操作。这与当初弃用 409
   * 的理由是同一条（不能让一次正常的重试变成报错）。
   *
   * WHY 只重试一次：真正过载时，无限重试会把负载再翻一倍，反而让恢复更慢；
   * 第二次仍被拒就如实告诉用户，把节奏交回给人。
   */
  async function submitRun(url, body) {
    const options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    };
    try {
      return await api(url, options);
    } catch (err) {
      if (err.status !== 429) throw err;
      const seconds = parseInt(err.retryAfter, 10);
      const wait = Math.max(1, Math.min(30, Number.isFinite(seconds) ? seconds : 5));
      appendNotice(`服务当前繁忙，${wait} 秒后自动重试…`);
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
      return await api(url, options);
    }
  }

  /**
   * 追加一条中性提示（非错误）。
   *
   * WHY 与错误分开：运行被系统终止（超时、审批过期）不是故障，用「错误：」
   * 前缀会让用户以为服务坏了，从而反复重试——而重试恰恰解决不了超时。
   */
  function appendNotice(message) {
    els.messages.appendChild(el('div', 'msg notice', message));
    scrollToBottom();
  }

  function appendToolCard(payload) {
    const card = el('div', 'tool');
    const head = el('div', 'tool-head');
    head.appendChild(el('span', 'tool-name', payload.name || 'unknown'));
    head.appendChild(el('span', null, '# ' + payload.index));

    const body = el('pre', 'tool-body');
    body.textContent = JSON.stringify(payload.args, null, 2);

    // 默认折叠，点击标题展开，避免长参数刷屏
    head.addEventListener('click', () => card.classList.toggle('open'));

    card.appendChild(head);
    card.appendChild(body);
    els.messages.appendChild(card);
    scrollToBottom();

    state.toolNodes.push({ index: payload.index, name: payload.name, body: body });
  }

  function appendToolResult(payload) {
    // 反向查找最近的、尚未填充结果的同名工具卡片
    for (let i = state.toolNodes.length - 1; i >= 0; i--) {
      const node = state.toolNodes[i];
      if (node.filled) continue;
      if (node.name && payload.name && node.name !== payload.name) continue;
      node.filled = true;
      node.body.textContent +=
        `\n--- 结果 ---\n${payload.preview}${payload.truncated ? '\n(输出已截断)' : ''}`;
      if (payload.full_output_ref) {
        // WHY 做成可点击文本而不是按钮：这是「想看全的人」的入口，不是每轮都要
        // 做的动作——做成按钮会让人以为必须点它对话才算完成。
        const link = el('span', 'ws-link', '\n查看完整输出');
        link.addEventListener('click', () => showFullOutput(payload.full_output_ref));
        node.body.appendChild(link);
      }
      return;
    }
    // 没找到对应卡片时退化成独立块，保证结果不丢
    const pre = el('pre', 'tool open-body');
    pre.textContent = `[${payload.name || 'tool'}]\n${payload.preview}`;
    els.messages.appendChild(pre);
    scrollToBottom();
  }

  /**
   * 渲染历史消息。
   * WHY 按 role 而不是按事件重放：历史来自检查点的最终状态，
   * 其中的 token 早已合并成完整文本，重放事件流既慢也没有对应数据。
   */
  function renderHistory(messages) {
    if (!messages.length) {
      els.messages.appendChild(
        el('div', 'thread-empty', '这是一个空会话，直接输入任务即可。')
      );
      return;
    }

    messages.forEach((message, order) => {
      const role = message.role || '';
      const content = typeof message.content === 'string' ? message.content : '';

      if (role === 'human') {
        const bubble = el('div', 'msg user', content);
        // WHY 用渲染下标当 message_index：历史按后端同一顺序渲染，两边不必再对一次 id
        const edit = el('span', 'msg-action', '编辑');
        edit.addEventListener('click', () => beginEdit(order, content));
        bubble.appendChild(edit);
        els.messages.appendChild(bubble);
        return;
      }

      if (role === 'tool') {
        const pre = el('pre', 'tool open-body');
        pre.textContent =
          `[${message.name || 'tool'}]\n` + content.slice(0, HISTORY_PREVIEW_LIMIT);
        els.messages.appendChild(pre);
        return;
      }

      // 助手消息可能只有工具调用、没有正文，因此两者独立判断
      if (content.trim()) {
        els.messages.appendChild(el('div', 'msg assistant', content));
      }
      (message.tool_calls || []).forEach((call, index) => {
        appendToolCard({
          name: call.name || 'unknown',
          args: call.args || {},
          index: `${order}.${index}`,
        });
      });
    });

    scrollToBottom();
  }

  /* ------------------------------------------------------------------ 审批 */

  /**
   * 渲染审批卡片。
   * WHY 按钮列表由 review_configs.allowed_decisions 动态决定：不同工具允许的
   * 决策不同（例如只有部分工具支持 edit），写死按钮会发出被后端拒绝的请求。
   */
  function renderApproval(payload) {
    const decisionMap = (payload.review_configs || []).reduce((acc, config) => {
      acc[config.action_name] = config.allowed_decisions || ['approve', 'reject'];
      return acc;
    }, {});

    const card = el('div', 'approval');
    card.appendChild(el('h4', null, '需要人工审批'));

    const requests = payload.action_requests || [];
    const decisions = new Array(requests.length);
    let resolved = 0;

    function maybeSubmit() {
      if (resolved !== requests.length) return;
      submitResume(requests.map((_, i) => decisions[i]), card);
    }

    requests.forEach((request, index) => {
      const item = el('div', 'approval-item');
      item.appendChild(el('div', null, `工具：${request.name}`));
      item.appendChild(el('div', null, `说明：${request.description || '无'}`));

      const argsBox = el('textarea', null);
      argsBox.rows = 4;
      argsBox.value = JSON.stringify(request.args || {}, null, 2);
      argsBox.style.width = '100%';
      argsBox.style.margin = '6px 0';
      item.appendChild(argsBox);

      const feedback = el('input', 'feedback');
      feedback.placeholder = '可选：补充说明（拒绝 / 代答时给模型的反馈）';
      item.appendChild(feedback);

      const allowed = decisionMap[request.name] || ['approve', 'reject'];
      const labelMap = {
        approve: '批准',
        reject: '拒绝',
        edit: '按修改后执行',
        respond: '我来代答',
      };

      const actions = el('div', 'approval-actions');
      allowed.forEach((type) => {
        const button = el('button', null, labelMap[type] || type);
        button.addEventListener('click', () => {
          const decision = { type: type };
          const message = feedback.value.trim();

          if (type === 'edit') {
            try {
              decision.edited_action = {
                name: request.name,
                args: JSON.parse(argsBox.value),
              };
            } catch (err) {
              appendError('参数 JSON 解析失败：' + err.message);
              return;
            }
          }
          if (type === 'reject' || type === 'respond') {
            decision.message = message || (type === 'reject' ? '用户拒绝了该调用' : '');
          }

          decisions[index] = decision;
          resolved += 1;
          // 该工具已决定，禁用本组按钮防止重复提交
          Array.from(actions.children).forEach((child) => {
            child.disabled = true;
          });
          maybeSubmit();
        });
        actions.appendChild(button);
      });

      item.appendChild(actions);
      card.appendChild(item);
    });

    if (!requests.length) {
      card.appendChild(el('div', null, '中断未提供可审批的调用，请刷新重试。'));
    }

    els.messages.appendChild(card);
    scrollToBottom();
  }

  async function submitResume(decisions, cardEl) {
    setRunning(true);
    try {
      const response = await api(`/api/threads/${state.threadId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decisions: decisions }),
      });
      cardEl.remove();
      await handleStream(response);
    } catch (err) {
      // WHY 审批过期要作废旧卡片：那一次中断已经被服务端作废，卡片留着会让
      // 用户以为「再点一次批准」还有用，而实际上服务端只会再回一次 409。
      if (err.status === 409) {
        cardEl.remove();
        appendNotice(err.message || '该审批已失效，请重新发起对话');
      } else {
        appendError(err.message);
      }
    } finally {
      setRunning(false);
    }
  }

  /* ------------------------------------------------------------------ 事件处理 */

  /** 渲染本轮 token 用量。 */
  function appendUsage(data) {
    if (!data) return;
    const prompt = Number(data.prompt_tokens) || 0;
    const completion = Number(data.completion_tokens) || 0;
    const total = Number(data.total_tokens) || prompt + completion;
    const text = `本轮用量 · 输入 ${prompt.toLocaleString()} / 输出 ${completion.toLocaleString()} / 合计 ${total.toLocaleString()} tokens`;

    // 正常情况下 usage 事件先于 done 到达，此时助手消息节点还在
    const node = el('div', 'msg usage', text);
    if (state.assistantEl) {
      state.assistantEl.appendChild(node);
    } else {
      els.messages.appendChild(node);
    }
    scrollToBottom();
  }

  function handleEvent(name, data) {
    switch (name) {
      case 'token':
        if (!state.assistantEl) {
          state.assistantEl = el('div', 'msg assistant');
          els.messages.appendChild(state.assistantEl);
        }
        state.assistantEl.textContent += data.text || '';
        scrollToBottom();
        break;

      case 'tool_call':
        appendToolCard(data);
        break;

      case 'tool_result':
        appendToolResult(data);
        break;

      case 'todos':
        renderTodos(data.items || []);
        break;

      case 'interrupt':
        renderApproval(data);
        break;

      case 'usage':
        // WHY 用量挂在助手消息之后而不是单独一行：它描述的就是「刚结束的这
        // 一轮花了多少」，脱离上下文摆放会被误读成整个会话的累计值。
        appendUsage(data);
        break;

      case 'error':
        appendError(data.message || '未知错误');
        break;

      case 'step':
        // 节点级进度仅用于兜底，不单独渲染
        break;

      case 'done':
        state.assistantEl = null;
        state.toolNodes = [];
        // 非正常收尾：流关闭但内容不完整，给出可见反馈而不是静默截断。
        // WHY 区分两种原因：用户自己按的停止他知道，而超时是系统替他终止的，
        // 若不点明「超时」，用户只会看到半截输出并以为界面卡了。
        if (data.reason === 'stopped') {
          appendNotice('本轮已停止');
        } else if (data.reason === 'timeout') {
          appendNotice('本轮已因超时被系统终止，请缩短任务或拆分为多轮');
        } else if (data.reason) {
          appendNotice(`本轮已结束（${data.reason}）`);
        }
        scrollToBottom();
        // WHY 结束时刷新清单：标题与最近活动时间正是在本轮结束时刷新的，
        // 不刷新则列表还停留在旧标题上
        loadThreads();
        break;

      default:
        break;
    }
  }

  function renderTodos(items) {
    const box = el('div', 'tool open');
    const head = el('div', 'tool-head');
    head.appendChild(el('span', 'tool-name', '待办清单'));
    box.appendChild(head);

    const body = el('pre', 'tool-body');
    body.textContent = items
      .map((item) => {
        const mark = item.status === 'completed' ? '[x]' : item.status === 'in_progress' ? '[>]' : '[ ]';
        return `${mark} ${item.content}`;
      })
      .join('\n');
    box.appendChild(body);
    els.messages.appendChild(box);
    scrollToBottom();
  }

  async function handleStream(response) {
    await consumeSSE(response, handleEvent);
  }

  /**
   * 请求服务端停止当前会话的运行。
   *
   * 只触发取消、不等流结束：SSE 流会继续推送已产出的事件，
   * 最终以 done(reason=stopped) 收尾，由 handleEvent 复位界面。
   * 后端对 not_running / already_stopping 均返回 200，无需在这里报错。
   */
  async function stopRun() {
    if (!state.running || !state.threadId) return;
    // 禁用防止连点；失败时恢复可点，成功则等流收尾后由 setRunning 复位
    els.stop.disabled = true;
    try {
      await api(`/api/threads/${encodeURIComponent(state.threadId)}/stop`, {
        method: 'POST',
      });
    } catch (err) {
      appendError('停止失败：' + err.message);
      els.stop.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ 交互 */

  async function send() {
    if (state.running) return;
    const content = els.input.value.trim();
    if (!content) return;

    setRunning(true);
    try {
      // WHY 先申请再渲染用户气泡：申请失败时输入内容仍留在输入框里，
      // 用户可以重试，而不是看到一条"发出去了却没被接受"的假消息
      await ensureThread();
      // 把会话写进 URL：此后刷新与分享都能回到这段对话
      navigate(`#/c/${state.threadId}`);

      const hint = els.messages.querySelector('.thread-empty');
      if (hint) hint.remove();

      els.messages.appendChild(el('div', 'msg user', content));
      els.input.value = '';
      els.input.style.height = 'auto';
      scrollToBottom();

      const editing = state.editTarget;
      const response = await submitRun(
        editing === null
          ? `/api/threads/${state.threadId}/runs`
          : `/api/threads/${state.threadId}/edit`,
        editing === null
          ? { content: content, model: els.modelSelect.value || null }
          : {
              message_index: editing,
              content: content,
              model: els.modelSelect.value || null,
            }
      );
      await handleStream(response);
      // WHY 分叉后重载而不是就地拼接：编辑与重新生成会切到一条**新的**分支，
      // 就地拼出来的画面仍是旧分支的历史加上新回复，看着像接错了上下文。
      if (editing !== null) {
        await loadThread(state.threadId);
      }
    } catch (err) {
      appendError(err.message);
    } finally {
      setRunning(false);
      await renderConversationControls();
    }
  }

  /** 进入编辑态：把原文本灌进输入框，发送时改为走 /edit 从该点分叉。 */
  function beginEdit(index, content) {
    state.editTarget = index;
    els.input.value = content;
    els.input.focus();
    renderConversationControls();
  }

  /** 重新生成最后一轮助手回复；与发送共用同一套流式处理。 */
  async function regenerate() {
    if (state.running || !state.threadId) return;
    setRunning(true);
    try {
      const response = await submitRun(`/api/threads/${state.threadId}/regenerate`, {
        model: els.modelSelect.value || null,
      });
      await handleStream(response);
      await loadThread(state.threadId);
    } catch (err) {
      appendError('重新生成失败：' + err.message);
    } finally {
      setRunning(false);
      await renderConversationControls();
    }
  }

  /**
   * 渲染分支栏与「重新生成」入口。
   *
   * WHY 每轮流结束都重画：分叉会改变当前分支，而这两个控件的内容都取决于它；
   * 少画一次界面就会停在操作前的分支上，用户会以为没生效。
   */
  async function renderConversationControls() {
    els.messages
      .querySelectorAll('.branch-bar, .regenerate-bar')
      .forEach((node) => node.remove());
    if (!state.threadId) return;

    let branches = null;
    try {
      const response = await api(`/api/threads/${state.threadId}/branches`);
      branches = await response.json();
    } catch (err) {
      // 分支栏是辅助信息：拉不到就不显示，不该打断正在进行的对话
      branches = null;
    }

    if (branches && (branches.items || []).length > 1) {
      const bar = el('div', 'branch-bar');
      bar.appendChild(el('span', 'branch-label', '分支：'));
      branches.items.forEach((item) => {
        const chip = el(
          'span',
          item.current ? 'branch-chip current' : 'branch-chip',
          item.label || '原始分支'
        );
        chip.addEventListener('click', () => activateBranch(item.branch_id));
        bar.appendChild(chip);
      });
      els.messages.insertBefore(bar, els.messages.firstChild);
    }

    const bar = el('div', 'regenerate-bar');
    const action = el(
      'span',
      'msg-action',
      state.editTarget === null ? '重新生成' : '取消编辑'
    );
    action.addEventListener('click', () => {
      if (state.editTarget === null) {
        regenerate();
        return;
      }
      state.editTarget = null;
      els.input.value = '';
      renderConversationControls();
    });
    bar.appendChild(action);
    els.messages.appendChild(bar);
  }

  /** 切换到指定分支并重载对话。 */
  async function activateBranch(branchId) {
    if (state.running) return;
    try {
      await api(
        `/api/threads/${state.threadId}/branches/activate?branch_id=${encodeURIComponent(branchId)}`,
        { method: 'POST' }
      );
      state.branch = branchId || null;
      await loadThread(state.threadId);
    } catch (err) {
      appendError('切换分支失败：' + err.message);
    }
  }

  async function loadModels() {
    try {
      const response = await api('/api/models');
      const models = await response.json();
      els.modelSelect.innerHTML = '';
      models.forEach((item) => {
        const option = el('option', null, `${item.name} (${item.provider})`);
        option.value = item.name;
        els.modelSelect.appendChild(option);
      });
    } catch (err) {
      appendError('模型列表加载失败：' + err.message);
    }
  }

  function bindEvents() {
    els.send.addEventListener('click', send);
    els.stop.addEventListener('click', stopRun);

    els.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });

    // 输入框随内容增高，最多到 CSS 中的 max-height
    els.input.addEventListener('input', () => {
      els.input.style.height = 'auto';
      els.input.style.height = els.input.scrollHeight + 'px';
    });

    els.newThread.addEventListener('click', () => {
      if (state.running) return;
      // 只切草稿态：不申请、不写库，会话在首条消息发出时才诞生
      navigate('');
    });

    // WHY 输入防抖：逐个字符发请求会在中文输入法下产生大量无意义查询，
    // 而每次查询都要在服务端扫一遍标题。
    let searchTimer = null;
    els.threadSearch.addEventListener('input', () => {
      if (searchTimer) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.threadFilter.query = els.threadSearch.value.trim();
        loadThreads();
      }, 300);
    });

    els.threadArchived.addEventListener('change', () => {
      state.threadFilter.includeArchived = els.threadArchived.checked;
      loadThreads();
    });
  }

  function renderAuth() {
    const container = els.authStatus;
    container.innerHTML = '';
    if (state.auth.mode === 'disabled') return;

    const principal = state.auth.principal;
    if (!principal) {
      const login = el('a', 'auth-link', '登录');
      login.href = state.auth.login_url || '/auth/login';
      container.appendChild(login);
      return;
    }

    const name = el('span', 'auth-name', principal.display_name || principal.user_id);
    container.appendChild(name);

    // 仅管理员显示管理入口
    if ((principal.permissions || []).includes('apikey:manage')) {
      const manage = el('a', 'auth-link', '管理');
      manage.href = '#';
      manage.addEventListener('click', (event) => {
        event.preventDefault();
        openAdminModal();
      });
      container.appendChild(manage);
    }

    const logout = el('a', 'auth-link', '退出');
    logout.href = '/auth/logout';
    container.appendChild(logout);
  }

  /* ------------------------------------------------------------------ 管理面板 */

  function openAdminModal() {
    els.adminModal.style.display = '';
    loadApiKeys();
  }

  function closeAdminModal() {
    els.adminModal.style.display = 'none';
  }

  function switchTab(target) {
    els.adminTabs.forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === target));
    els.tabPanels.forEach((panel) => panel.classList.toggle('active', panel.id === `tab-${target}`));
    if (target === 'audit') loadAudit();
  }

  async function loadApiKeys() {
    try {
      const response = await api('/auth/api-keys');
      state.admin.apikeys = await response.json();
      renderApiKeys();
    } catch (err) {
      appendError('API Key 列表加载失败：' + err.message);
    }
  }

  function renderApiKeys() {
    els.apikeyList.innerHTML = '';
    if (!state.admin.apikeys.length) {
      els.apikeyList.appendChild(el('div', 'admin-empty', '暂无 API Key'));
      return;
    }
    state.admin.apikeys.forEach((item) => {
      const row = el('div', 'apikey-item');
      const meta = el('div', 'apikey-meta');
      const info = [
        `ID: ${item.key_id}`,
        `角色: ${item.role}`,
        `前缀: ${item.key_prefix}`,
        item.description || '无描述',
        item.revoked_at ? `已吊销 ${formatTime(item.revoked_at)}` : `创建于 ${formatTime(item.created_at)}`,
      ].join(' · ');
      meta.textContent = info;
      row.appendChild(meta);

      if (!item.revoked_at) {
        const revokeBtn = el('button', 'danger small', '吊销');
        revokeBtn.addEventListener('click', () => revokeApiKey(item.key_id));
        row.appendChild(revokeBtn);
      }
      els.apikeyList.appendChild(row);
    });
  }

  async function createApiKey(event) {
    event.preventDefault();
    const role = document.getElementById('apikey-role').value;
    const scopes = document.getElementById('apikey-scopes').value;
    const description = document.getElementById('apikey-desc').value;
    const expiresAt = document.getElementById('apikey-expires').value;

    try {
      const params = new URLSearchParams();
      params.append('role', role);
      if (scopes) params.append('scopes', scopes);
      if (description) params.append('description', description);
      if (expiresAt) params.append('expires_at', expiresAt);

      const response = await api('/auth/api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params,
      });
      const result = await response.json();
      await loadApiKeys();
      showApiKeyPopup(result.key);
      els.apikeyForm.reset();
    } catch (err) {
      appendError('创建 API Key 失败：' + err.message);
    }
  }

  async function revokeApiKey(keyId) {
    if (!confirm(`确认吊销 API Key ${keyId}？`)) return;
    try {
      await api(`/auth/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE' });
      await loadApiKeys();
    } catch (err) {
      appendError('吊销 API Key 失败：' + err.message);
    }
  }

  function showApiKeyPopup(key) {
    els.apikeyValue.value = key;
    els.apikeyPopup.style.display = '';
    els.copyMsg.textContent = '';
  }

  function closeApiKeyPopup() {
    els.apikeyPopup.style.display = 'none';
    els.apikeyValue.value = '';
  }

  async function copyApiKey() {
    try {
      await navigator.clipboard.writeText(els.apikeyValue.value);
      els.copyMsg.textContent = '已复制';
    } catch (err) {
      els.copyMsg.textContent = '复制失败，请手动复制';
    }
  }

  async function loadAudit() {
    try {
      const response = await api('/auth/audit?limit=100');
      const rows = await response.json();
      renderAudit(rows);
    } catch (err) {
      appendError('审计日志加载失败：' + err.message);
    }
  }

  function renderAudit(rows) {
    els.auditTableBody.innerHTML = '';
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td');
      td.colSpan = 6;
      td.textContent = '暂无记录';
      td.className = 'audit-empty';
      tr.appendChild(td);
      els.auditTableBody.appendChild(tr);
      return;
    }
    rows.forEach((row) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, formatTime(row.created_at)));
      tr.appendChild(el('td', null, row.event_type));
      tr.appendChild(el('td', null, row.actor_id));
      tr.appendChild(el('td', null, row.action || ''));
      tr.appendChild(el('td', null, row.outcome));
      tr.appendChild(el('td', null, row.ip || ''));
      els.auditTableBody.appendChild(tr);
    });
  }

  function bindAdminEvents() {
    els.adminClose.addEventListener('click', closeAdminModal);
    els.popupClose.addEventListener('click', closeApiKeyPopup);
    els.apikeyCopy.addEventListener('click', copyApiKey);
    els.auditRefresh.addEventListener('click', () => loadAudit());

    els.adminTabs.forEach((tab) => {
      tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    if (els.apikeyForm) {
      els.apikeyForm.addEventListener('submit', createApiKey);
    }

    // 点击遮罩关闭弹窗
    els.adminModal.querySelector('.modal-backdrop').addEventListener('click', closeAdminModal);
    els.apikeyPopup.querySelector('.modal-backdrop').addEventListener('click', closeApiKeyPopup);
  }

  /* ------------------------------------------------------------------ 长期记忆面板 */

  /**
   * 把记忆路径拼成接口 URL。
   * WHY 逐段编码：路径里可能是中文（模型常写「偏好.md」），而整体
   * encodeURIComponent 会把分隔符 `/` 一并编码，后端就只能拿到一个被压成
   * 单段的路径。
   */
  function memoryUrl(path) {
    return `/api/memories${String(path)
      .split('/')
      .map((segment) => encodeURIComponent(segment))
      .join('/')}`;
  }

  function openMemoryModal() {
    els.memoryModal.style.display = '';
    loadMemories();
  }

  function closeMemoryModal() {
    els.memoryModal.style.display = 'none';
  }

  async function loadMemories() {
    els.memoryList.innerHTML = '';
    els.memoryList.appendChild(el('div', 'memory-empty', '加载中…'));
    try {
      const response = await api('/api/memories');
      const payload = await response.json();
      state.memories = { items: payload.items || [], truncated: !!payload.truncated };
      renderMemories();
    } catch (err) {
      state.memories = { items: [], truncated: false };
      els.memoryList.innerHTML = '';
      els.memoryList.appendChild(el('div', 'memory-empty', `加载失败：${err.message}`));
    }
  }

  function renderMemories() {
    const container = els.memoryList;
    container.innerHTML = '';
    const items = state.memories.items;

    if (!items.length) {
      container.appendChild(
        el('div', 'memory-empty', '还没有记忆。Agent 在对话中记下的长期偏好会出现在这里。')
      );
      return;
    }

    // WHY 显式提示截断：被截断的清单与「记忆本来就少」在界面上无法区分，
    // 用户会直接理解成「我的记忆丢了」。
    if (state.memories.truncated) {
      container.appendChild(el('div', 'memory-empty', '条目较多，仅显示前一部分。'));
    }

    items.forEach((item) => {
      const row = el('div', 'memory-item');
      const main = el('div', 'm-main');
      main.appendChild(el('div', 'm-path', item.path));
      main.appendChild(el('div', 'm-content', item.content || '（空内容）'));

      const sub = [formatTime(item.updated_at)];
      if (item.truncated) sub.push('内容已截断');
      main.appendChild(el('div', 'm-sub', sub.filter(Boolean).join(' · ')));
      row.appendChild(main);

      const remove = el('button', 'danger small', '删除');
      remove.type = 'button';
      remove.addEventListener('click', () => removeMemory(item));
      row.appendChild(remove);
      container.appendChild(row);
    });
  }

  async function removeMemory(item) {
    // WHY 二次确认：删除不可撤销（服务端只记审计、不保留副本），而记忆会进入
    // 后续每一轮上下文——误删的代价远高于多点一次确认。
    if (!window.confirm(`删除这条记忆？\n${item.path}`)) return;
    try {
      await api(memoryUrl(item.path), { method: 'DELETE' });
      await loadMemories();
    } catch (err) {
      window.alert(`删除失败：${err.message}`);
    }
  }

  function bindMemoryEvents() {
    els.memoryOpen.addEventListener('click', openMemoryModal);
    els.memoryClose.addEventListener('click', closeMemoryModal);
    els.memoryRefresh.addEventListener('click', () => loadMemories());
    els.memoryModal.querySelector('.modal-backdrop').addEventListener('click', closeMemoryModal);
  }

  /* ------------------------------------------------------------------ 工作区面板 */

function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function openWorkspaceModal() {
  els.workspaceModal.style.display = '';
  if (!state.workspace.dirs['/']) {
    loadDirectory('/');
  } else {
    renderTree();
  }
}

function closeWorkspaceModal() {
  els.workspaceModal.style.display = 'none';
}

async function loadDirectory(path) {
  try {
    // WHY 编码整个 path 作为一个查询参数：虚拟路径里可能含中文与空格，
    // 而这里它是 query value，不是路径段——整体编码才是正确做法。
    const response = await api(`/api/workspace/files?path=${encodeURIComponent(path)}`);
    const payload = await response.json();
    state.workspace.dirs[payload.path] = {
      entries: payload.entries || [],
      truncated: !!payload.truncated,
    };
    renderTree();
  } catch (err) {
    state.workspace.dirs[path] = { entries: [], truncated: false, error: err.message };
    renderTree();
  }
}

async function toggleDir(path) {
  const expanded = !state.workspace.expanded[path];
  state.workspace.expanded[path] = expanded;
  if (expanded && !state.workspace.dirs[path]) {
    renderTree(); // 先画出「加载中」，避免点了没反应
    await loadDirectory(path);
    return;
  }
  renderTree();
}

function buildTreeRows(path, depth) {
  const group = el('div', 'ws-group');
  const node = state.workspace.dirs[path];
  const entries = node ? node.entries : [];

  entries.forEach((entry) => {
    const row = el('div', 'ws-row');
    row.style.paddingLeft = `${8 + depth * 14}px`;
    if (entry.is_dir) {
      row.appendChild(
        el('span', 'ws-marker', state.workspace.expanded[entry.path] ? '▾' : '▸')
      );
      row.appendChild(el('span', 'ws-name', entry.name));
      row.addEventListener('click', () => toggleDir(entry.path));
      group.appendChild(row);
      if (state.workspace.expanded[entry.path]) {
        if (state.workspace.dirs[entry.path]) {
          group.appendChild(buildTreeRows(entry.path, depth + 1));
        } else {
          const loading = el('div', 'ws-row ws-muted', '加载中…');
          loading.style.paddingLeft = `${8 + (depth + 1) * 14}px`;
          group.appendChild(loading);
        }
      }
      return;
    }
    row.appendChild(el('span', 'ws-marker', ' '));
    row.appendChild(el('span', 'ws-name', entry.name));
    row.appendChild(el('span', 'ws-size', formatSize(entry.size)));
    if (entry.path === state.workspace.selected) row.classList.add('active');
    row.addEventListener('click', () => openWorkspaceFile(entry.path));
    group.appendChild(row);
  });

  if (node && node.truncated) {
    const more = el('div', 'ws-row ws-muted', '（条目较多，仅显示前一部分）');
    more.style.paddingLeft = `${8 + depth * 14}px`;
    group.appendChild(more);
  }
  if (node && node.error) {
    const failed = el('div', 'ws-row ws-muted', `读取失败：${node.error}`);
    failed.style.paddingLeft = `${8 + depth * 14}px`;
    group.appendChild(failed);
  }
  return group;
}

function renderTree() {
  els.workspaceTree.innerHTML = '';
  els.workspaceTree.appendChild(buildTreeRows('/', 0));
}

async function openWorkspaceFile(path) {
  state.workspace.selected = path;
  // 展开并加载父目录，让用户能看出「我打开的这份文件在哪」
  const parent = path.split('/').slice(0, -1).join('/') || '/';
  state.workspace.expanded[parent] = true;
  if (!state.workspace.dirs[parent]) await loadDirectory(parent);
  renderTree();

  els.workspacePreview.innerHTML = '';
  els.workspacePreview.appendChild(el('div', 'workspace-empty', '加载中…'));
  try {
    const response = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`);
    renderFile(await response.json());
  } catch (err) {
    els.workspacePreview.innerHTML = '';
    els.workspacePreview.appendChild(
      el('div', 'workspace-empty', `打开失败：${err.message}`)
    );
  }
}

function renderFile(payload) {
  const box = els.workspacePreview;
  box.innerHTML = '';

  const head = el('div', 'ws-file-head');
  head.appendChild(el('div', 'ws-file-name', payload.name));
  const sub = [formatSize(payload.size)];
  if (payload.mime_type) sub.push(payload.mime_type);
  if (payload.truncated) sub.push('仅显示前一部分');
  head.appendChild(el('div', 'ws-file-sub', sub.filter(Boolean).join(' · ')));
  box.appendChild(head);

  if (payload.kind === 'image') {
    const img = document.createElement('img');
    img.className = 'ws-image';
    img.src = payload.text; // 后端给的 data URL，仅对图片类型填充
    img.alt = payload.name;
    box.appendChild(img);
    return;
  }
  if (payload.kind === 'binary') {
    box.appendChild(el('div', 'workspace-empty', '这不是文本文件，无法在面板里预览。'));
    return;
  }
  if (payload.kind === 'too_large') {
    box.appendChild(
      el('div', 'workspace-empty', '文件过大，未做预览（以免把浏览器卡住）。')
    );
    return;
  }
  const pre = el('pre', 'ws-text', payload.text || '（空文件）');
  box.appendChild(pre);

  if (payload.kind === 'text' && payload.truncated) {
    // WHY 续取而不是提高预览上限：一次塞进几十万字符会把浏览器拖住，而提高上限
    // 只是把同一个问题推给下一次更大的输出。「完整查看」由分段拼出来。
    state.workspace.text = {
      path: payload.path,
      offset: (payload.offset || 0) + (payload.text || '').length,
    };
    const more = el('button', 'ws-more', `加载更多（已显示 ${state.workspace.text.offset} 字符）`);
    more.type = 'button';
    more.addEventListener('click', () => loadMoreFile(more, pre));
    box.appendChild(more);
  } else {
    state.workspace.text = null;
  }
}

/** 续取下一页并追加到预览区。 */
async function loadMoreFile(button, pre) {
  const cursor = state.workspace.text;
  if (!cursor) return;
  button.disabled = true;
  button.textContent = '加载中…';
  try {
    const response = await api(
      `/api/workspace/file?path=${encodeURIComponent(cursor.path)}&offset=${cursor.offset}`
    );
    const payload = await response.json();
    pre.textContent += payload.text || '';
    cursor.offset = (payload.offset || 0) + (payload.text || '').length;
    if (payload.truncated) {
      button.disabled = false;
      button.textContent = `加载更多（已显示 ${cursor.offset} 字符）`;
    } else {
      button.remove();
      els.workspacePreview.appendChild(
        el('div', 'ws-file-sub', `已显示全部 ${cursor.offset} 字符`)
      );
    }
  } catch (err) {
    // 失败时保留按钮：这是可以重试的动作，把入口弄没等于让人重新找文件
    button.disabled = false;
    button.textContent = `加载更多（失败：${err.message}）`;
  }
}

/** 从工具卡片跳到完整输出。 */
async function showFullOutput(ref) {
  openWorkspaceModal();
  await openWorkspaceFile(ref);
}

function bindWorkspaceEvents() {
  els.workspaceOpen.addEventListener('click', openWorkspaceModal);
  els.workspaceClose.addEventListener('click', closeWorkspaceModal);
  els.workspaceModal.querySelector('.modal-backdrop').addEventListener('click', closeWorkspaceModal);
}

async function loadAuth() {
    try {
      const cfgResponse = await api('/auth/config');
      const cfg = await cfgResponse.json();
      state.auth.mode = cfg.auth_mode || 'disabled';
      state.auth.login_url = cfg.login_url;

      if (state.auth.mode !== 'disabled') {
        try {
          const meResponse = await api('/auth/me');
          state.auth.principal = await meResponse.json();
        } catch (err) {
          // /auth/me 401 属于正常未登录态，无需报错
          state.auth.principal = null;
        }
      }
    } catch (err) {
      // 认证配置读取失败不应阻塞主界面
      state.auth.mode = 'disabled';
    }
    renderAuth();
  }

  async function init() {
    // WHY 先加载认证配置：后续所有 API 调用都依赖 401 处理逻辑
    await loadAuth();

    bindEvents();
    bindAdminEvents();
    bindMemoryEvents();
    bindWorkspaceEvents();

    // WHY 只注册不直接调用：navigate() 赋值 hash 同样会触发该事件，
    // 让「URL 变化 → 同步界面」成为唯一入口，避免两处逻辑漂移
    window.addEventListener('hashchange', () => {
      syncWithUrl();
    });

    await loadModels();
    await loadThreads();
    // 刷新时按 URL 恢复：带会话 ID 则拉历史，否则进入草稿态（不创建任何东西）
    await syncWithUrl();
    els.input.focus();
  }

  init();
})();
