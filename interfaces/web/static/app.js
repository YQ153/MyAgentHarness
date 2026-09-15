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
    newThread: document.getElementById('new-thread'),
    modelSelect: document.getElementById('model-select'),
    threadList: document.getElementById('thread-list'),
  };

  const state = {
    threadId: null,
    running: false,
    assistantEl: null,
    toolNodes: [],
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
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (err) {
        /* 响应体不是 JSON 时保持默认文案 */
      }
      throw new Error(detail);
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

  function renderThreads(items) {
    els.threadList.innerHTML = '';

    if (!items.length) {
      els.threadList.appendChild(el('div', 'thread-empty', '暂无历史会话'));
      return;
    }

    items.forEach((item) => {
      const node = el('div', 'thread-item');
      node.dataset.threadId = item.thread_id;
      node.appendChild(el('div', 't-title', item.title || '未命名会话'));

      const sub = [formatTime(item.updated_at)];
      if (item.turn_count > 0) sub.push(`${item.turn_count} 轮`);
      node.appendChild(el('div', 't-sub', sub.filter(Boolean).join(' · ')));

      node.addEventListener('click', () => {
        // 运行中禁止切换：事件流绑定在当前会话上，换 ID 会把输出渲染进错误的窗口
        if (state.running) return;
        navigate(`#/c/${item.thread_id}`);
      });
      els.threadList.appendChild(node);
    });

    markActiveThread();
  }

  async function loadThreads() {
    try {
      const response = await api(`/api/threads?limit=${THREAD_PAGE_SIZE}`);
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
    els.messages.innerHTML = '';
    markActiveThread();

    try {
      const response = await api(`/api/threads/${encodeURIComponent(threadId)}`);
      const messages = await response.json();
      renderHistory(messages);
    } catch (err) {
      appendError('加载会话历史失败：' + err.message);
    }
  }

  /* ------------------------------------------------------------------ 渲染 */

  function appendError(message) {
    els.messages.appendChild(el('div', 'msg error', `错误：${message}`));
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
        els.messages.appendChild(el('div', 'msg user', content));
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
      appendError(err.message);
    } finally {
      setRunning(false);
    }
  }

  /* ------------------------------------------------------------------ 事件处理 */

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

      case 'error':
        appendError(data.message || '未知错误');
        break;

      case 'step':
        // 节点级进度仅用于兜底，不单独渲染
        break;

      case 'done':
        state.assistantEl = null;
        state.toolNodes = [];
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

      const response = await api(`/api/threads/${state.threadId}/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: content, model: els.modelSelect.value || null }),
      });
      await handleStream(response);
    } catch (err) {
      appendError(err.message);
    } finally {
      setRunning(false);
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
  }

  async function init() {
    bindEvents();

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
