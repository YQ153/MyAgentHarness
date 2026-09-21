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
    auditOpen: document.getElementById('audit-open'),
    auditModal: document.getElementById('audit-modal'),
    auditClose: document.getElementById('audit-close'),
    auditRefresh: document.getElementById('audit-refresh'),
    auditTableBody: document.querySelector('#audit-table tbody'),
    memoryOpen: document.getElementById('memory-open'),
    memoryModal: document.getElementById('memory-modal'),
    memoryClose: document.getElementById('memory-close'),
    memoryRefresh: document.getElementById('memory-refresh'),
    memoryList: document.getElementById('memory-list'),
    workspaceOpen: document.getElementById('workspace-open'),
    workspacePicker: document.getElementById('workspace-picker'),
    workspaceChoiceText: document.getElementById('workspace-choice'),
    workspaceClear: document.getElementById('workspace-clear'),
    workspacePick: document.getElementById('workspace-pick'),
    workspacePickStatus: document.getElementById('workspace-pick-status'),
    workspaceBrowseOpen: document.getElementById('workspace-browse'),
    workspaceBrowseModal: document.getElementById('workspace-browse-modal'),
    workspaceBrowseClose: document.getElementById('workspace-browse-close'),
    workspaceBrowseInput: document.getElementById('workspace-browse-input'),
    workspaceBrowseGo: document.getElementById('workspace-browse-go'),
    workspaceBrowseUp: document.getElementById('workspace-browse-up'),
    workspaceBrowsePath: document.getElementById('workspace-browse-path'),
    workspaceBrowseList: document.getElementById('workspace-browse-list'),
    workspaceBrowseUse: document.getElementById('workspace-browse-use'),
    workspaceModal: document.getElementById('workspace-modal'),
    workspaceClose: document.getElementById('workspace-close'),
    workspacePath: document.getElementById('workspace-path'),
    workspaceTree: document.getElementById('workspace-tree'),
    workspacePreview: document.getElementById('workspace-preview'),
    knowledgeOpen: document.getElementById('knowledge-open'),
    knowledgeModal: document.getElementById('knowledge-modal'),
    knowledgeClose: document.getElementById('knowledge-close'),
    knowledgeRefresh: document.getElementById('knowledge-refresh'),
    knowledgeIndex: document.getElementById('knowledge-index'),
    knowledgeCaps: document.getElementById('knowledge-caps'),
    knowledgeList: document.getElementById('knowledge-list'),
    skillsOpen: document.getElementById('skills-open'),
    skillsModal: document.getElementById('skills-modal'),
    skillsClose: document.getElementById('skills-close'),
    skillsRefresh: document.getElementById('skills-refresh'),
    skillsCaps: document.getElementById('skills-caps'),
    skillsList: document.getElementById('skills-list'),
    attach: document.getElementById('attach'),
    attachInput: document.getElementById('attach-input'),
    attachmentStrip: document.getElementById('attachment-strip'),
    attachmentHint: document.getElementById('attachment-hint'),
  };

  const state = {
    threadId: null,
    running: false,
    assistantEl: null,
    /** 助手气泡里的正文容器；用量标签与工具卡片是它的兄弟节点。 */
    assistantBody: null,
    toolNodes: [],
    /** 会话清单的过滤条件；与界面控件保持一致，刷新清单时统一从这里取。 */
    threadFilter: { query: '', includeArchived: false },
    memories: { items: [], truncated: false },
    /**
     * 工作区目录树状态（含根作用域代号 ``generation``）。
     *
     * WHY 只存「已加载的目录」而不是整棵树：面板按需展开下一层，未展开的目录
     * 不该存在于客户端状态里——否则「树」与「服务端实际情况」就有两份真相。
     *
     * WHY 它由 WorkspaceScope 造而不是写成一个字面量：这四份缓存**只对一条会话的根成立**
     * （``dirs`` 甚至按虚拟路径缓存，而 ``/`` 在两条会话下都合法却指向不同目录），换会话
     * 必须整份丢掉。把「它长什么样」交给那个模块，规则与它的用例就是同一份定义。
     */
    workspace: WorkspaceScope.begin(null),
    /**
     * 本次新建会话选择的工作空间；``null`` 表示**不绑定**。
     *
     * WHY 选择只存在本地、直到首条消息才交给服务端：文件根在会话真正确立的那一刻锁定
     * （服务端以首条消息上的取值为准）。草稿态还不存在会话，此时提交只会造出一条
     * 「有根但没有内容」的记录。
     *
     * WHY 用 ``null`` 而不是某个默认路径表示「不绑定」：不绑定是一种**正常结果**——
     * 这条会话将使用应用为它创建的专属目录，而不是退回某个项目目录。给一个默认路径，
     * 等于把「用户没选」偷偷变成「用户选了配置里那个」，而两者本该落到不同的根上。
     */
    workspaceChoice: null,
    /**
     * 浏览弹窗的当前一层：``{path, parent, roots, entries}``。
     *
     * WHY 只存一层而不是把走过的路都留下：服务端每一步都重新算（它才是边界的所有者，
     * 而且目录可能随时被删）。客户端缓存整棵树只会造出第二份事实。
     */
    workspaceBrowse: { path: '', parent: null, roots: [], entries: [] },
    /**
     * 是否正在等系统文件夹弹窗返回。
     *
     * WHY 需要它：这一步要等用户关掉一个**在服务端屏幕上**的窗口（最长几分钟），期间
     * 请求是挂着的。不锁住按钮，用户会以为没反应而连点，于是服务端叠出第二个对话框
     * ——而他并不知道自己开了两个。
     */
    workspacePickPending: false,
    /** 编辑态：待改写的用户消息下标；null 表示正常发送新消息。 */
    editTarget: null,
    /** 正在查看的分支标识；null 表示会话的当前分支。 */
    branch: null,
    /**
     * 待发送附件：{file, url, status, id, error}。
     *
     * WHY 保存在本地而不是选完就上传：草稿态还没有会话（会话在首条消息发出时
     * 才诞生），而上传接口的路径里必须带会话 ID。等到发送时再传，既避免了
     * 提前建会话，也让「取消发送」不会在工作区里留下孤儿附件。
     */
    attachments: [],
    /** 附件上限；来自 /api/attachments/limits。取不到时为 null，此时不做本地预校验。 */
    attachLimits: null,
    /** 模型别名 → 是否接受图片输入；来自 /api/models。 */
    modelVision: {},
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

  /**
   * 给面板类请求补上「看哪个工作区」。
   *
   * WHY 需要这一层：工作区在会话级可选之后，文件面板、附件、技能与知识库都按工作区
   * 各有一份。不带这个参数，服务端只能按启动默认工作区作答——于是 B 会话的面板显示
   * A 项目的文件，而两边都不会报错，用户只会发现「Agent 说改好了，面板里看不到」。
   *
   * 有会话时带 `thread_id`（服务端按绑定值解析，此时**不能**再带 workspace：一旦与
   * 绑定值不同就会被 409 拒绝）；草稿态带 `workspace`（这条会话还没绑定，取用户当前的
   * 选择）——两种情形互相排斥，正是这里分叉的原因。
   *
   * WHY 统一走一个函数而不是各调用点自己拼：本文件有近十处面板请求，漏一处的表现是
   * 「某一个面板指向了别的项目」——这正是最不容易被联想到调用点的一类症状。
   */
  function scoped(path) {
    const separator = path.includes('?') ? '&' : '?';
    if (state.threadId) {
      return path + separator + 'thread_id=' + encodeURIComponent(state.threadId);
    }
    if (!state.workspaceChoice) return path;
    return path + separator + 'workspace=' + encodeURIComponent(state.workspaceChoice);
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
    // 回到草稿态 = 要新建会话，此时工作区**尚未**绑定，选择器该出现（若有多项可选）
    renderWorkspacePicker();
    // 退回草稿态同样换了根（落回「本次选定的工作空间」，或还没有根）。
    syncWorkspaceScope();
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

  /**
   * 加载历史会话的消息并重绘。
   *
   * Args:
   *   threadId: 要打开的会话。
   *   force: 允许在 `state.running` 为真时重载。默认 `false` —— 守卫的用途是
   *     「运行中不要切走会话」，否则流式渲染会被清掉。
   *
   * WHY 需要 `force`：本轮流的**收尾**处（编辑分叉、重新生成之后）也要重载，
   * 而那一刻 `state.running` 仍为真（`setRunning(false)` 在 `finally` 里）。
   * 如果没有这个开关，那三处调用就只能二选一：要么被守卫挡成静默空转（界面停在
   * 旧分支上，用户以为操作没生效），要么去掉守卫、把「运行中切会话」这个保护一并
   * 丢掉。`force` 把「同一会话的收尾重载」与「切到另一个会话」这两种意图分开。
   */
  async function openThread(threadId, { force = false } = {}) {
    if (!threadId) return;
    if (state.running && !force) return;

    state.threadId = threadId;
    state.assistantEl = null;
    state.toolNodes = [];
    state.editTarget = null;
    els.messages.innerHTML = '';
    markActiveThread();
    // 打开既有会话 = 工作区已经绑定且不可变更，选择器该收起来（路径改在「工作区」面板里看）
    renderWorkspacePicker();
    // 换会话就是换文件根：面板的路径、悬停提示、目录树与预览都必须跟着这条会话走。
    syncWorkspaceScope();

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
        const refs = (message.attachments || []).map((item) => ({
          path: item.path,
          alt: item.filename,
        }));
        const bubble = el('div', 'msg user');
        if (refs.length) bubble.appendChild(renderAttachmentThumbs(refs));
        if (content) bubble.appendChild(el('div', 'user-text', content));
        // WHY 用渲染下标当 message_index：历史按后端同一顺序渲染，两边不必再对一次 id
        const edit = el('span', 'msg-action', '编辑');
        edit.addEventListener('click', () => beginEdit(order, content));
        bubble.appendChild(edit);
        els.messages.appendChild(bubble);
        // WHY 先渲染再逐张取图：等所有图片取完才渲染会让一段长历史卡在一个慢请求上
        if (refs.length) loadHistoryAttachmentImages(bubble, refs);
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
        const bubble = el('div', 'msg assistant');
        const body = el('div', 'md-body');
        // WHY 这里可以用 innerHTML：渲染器先整段转义、再只插入它自己白名单里的标签，
        // 链接地址还过一道 scheme 白名单。它不可用时退回纯文本——绝不能把原始文本
        // 直接塞进 innerHTML，那正是这次渲染要避开的 XSS 面。
        if (window.Markdown) {
          body.innerHTML = window.Markdown.render(content);
        } else {
          body.textContent = content;
        }
        bubble.appendChild(body);
        els.messages.appendChild(bubble);
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
          // 正文单独一个容器：用量小标签与工具卡片挂在气泡上、是它的兄弟节点，
          // 收尾渲染正文时只覆盖这一个节点，才不会把它们一起抹掉。
          state.assistantBody = el('div', 'md-body');
          state.assistantEl.appendChild(state.assistantBody);
          els.messages.appendChild(state.assistantEl);
        }
        // WHY 流式期间保持纯文本：每个 token 重渲染一次，会把未闭合的 ``` 反复解析，
        // 既慢又让人看到排版来回跳动；收尾时渲染一次，代价只付一遍。
        state.assistantBody.textContent += data.text || '';
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
        // 收尾时把整段正文渲染成 Markdown。转义与链接白名单都在渲染器里，
        // 因此这里可以安全地使用 innerHTML；渲染器缺席时保留纯文本。
        if (state.assistantBody && window.Markdown) {
          state.assistantBody.innerHTML = window.Markdown.render(
            state.assistantBody.textContent
          );
        }
        state.assistantEl = null;
        state.assistantBody = null;
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

  /* ------------------------------------------------------------------ 附件 */

  async function loadAttachmentLimits() {
    try {
      const response = await api('/api/attachments/limits');
      state.attachLimits = await response.json();
    } catch (err) {
      // WHY 拿不到上限不挡住发消息：本地预校验只是为了省一次往返，真正的判定
      // 始终在服务端。把「上限查询失败」升级成「不能发消息」是拿一个次要故障
      // 换掉主要功能。
      state.attachLimits = null;
    }
  }

  function applyLocalValidation(entry) {
    const limits = state.attachLimits;
    if (!limits) {
      entry.status = 'ready';
      entry.error = '';
      return;
    }
    if (entry.file.size > limits.max_bytes) {
      entry.status = 'failed';
      entry.error = `超过 ${Math.round(limits.max_bytes / 1024)} KB`;
      return;
    }
    if (limits.allowed_mime_types.indexOf(entry.file.type) < 0) {
      entry.status = 'failed';
      entry.error = '不支持的类型';
      return;
    }
    entry.status = 'ready';
    entry.error = '';
  }

  function pickFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const limits = state.attachLimits;
    const max = limits ? limits.max_per_thread : Infinity;
    files.forEach((file) => {
      const entry = {
        file: file,
        url: URL.createObjectURL(file),
        status: 'ready',
        id: null,
        error: '',
      };
      // 张数上限在选中时就要判：否则用户会一路选到发送时才被告知「太多了」
      if (state.attachments.length >= max) {
        entry.status = 'failed';
        entry.error = `最多 ${max} 张`;
      } else {
        applyLocalValidation(entry);
      }
      state.attachments.push(entry);
    });
    renderAttachmentStrip();
  }

  function retryAttachment(index) {
    const entry = state.attachments[index];
    if (!entry) return;
    applyLocalValidation(entry);
    renderAttachmentStrip();
  }

  function removeAttachment(index) {
    const entry = state.attachments[index];
    if (!entry) return;
    // 只有从未上传成功的条目才回收 object URL：已发送的条目其 URL 正被消息
    // 气泡里的 <img> 引用着，回收会让那条消息的图片当场变成裂图
    if (entry.status !== 'uploaded') URL.revokeObjectURL(entry.url);
    state.attachments.splice(index, 1);
    renderAttachmentStrip();
  }

  function renderAttachmentStrip() {
    const strip = els.attachmentStrip;
    strip.innerHTML = '';
    if (!state.attachments.length) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;

    state.attachments.forEach((entry, index) => {
      const chip = el('div', 'attach-chip' + (entry.status === 'failed' ? ' failed' : ''));
      const img = el('img');
      img.src = entry.url;
      img.alt = entry.file.name;
      chip.appendChild(img);

      const remove = el('button', 'attach-remove', '×');
      remove.type = 'button';
      remove.title = '移除';
      remove.addEventListener('click', () => removeAttachment(index));
      chip.appendChild(remove);

      if (entry.status === 'failed') {
        const retry = el('button', 'attach-retry', '↻');
        retry.type = 'button';
        retry.title = '重试';
        retry.addEventListener('click', () => retryAttachment(index));
        chip.appendChild(retry);
        chip.appendChild(el('div', 'attach-state', entry.error || '失败'));
      } else if (entry.status === 'uploading') {
        chip.appendChild(el('div', 'attach-state', '上传中'));
      }
      strip.appendChild(chip);
    });
  }

  async function uploadOne(threadId, entry, workspace) {
    const form = new FormData();
    form.append('file', entry.file, entry.file.name);
    // WHY 不手写 Content-Type：multipart 的 boundary 必须由浏览器生成，自己拼头
    // 会得到一个缺 boundary 的类型串，服务端解析直接失败。
    //
    // WHY 上传时要带上工作区：附件是在**首条消息之前**上传的，那一刻会话还没绑定工作区，
    // 服务端只能按启动默认值落盘——而这条会话可能运行在另一个工作区里，附件当场失效
    // （「附件不存在」，而它明明刚上传成功）。带上选择值之后，上传与运行落在同一个根上。
    const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : '';
    const response = await api(`/api/threads/${threadId}/attachments${query}`, {
      method: 'POST',
      body: form,
    });
    return response.json();
  }

  async function uploadPendingAttachments(threadId, workspace) {
    const pending = state.attachments.filter((entry) => !entry.id);
    for (const entry of pending) {
      if (entry.status === 'failed') {
        throw new Error(`附件「${entry.file.name}」未通过校验：${entry.error}`);
      }
      entry.status = 'uploading';
      renderAttachmentStrip();
      try {
        const info = await uploadOne(threadId, entry, workspace);
        entry.id = info.id;
        entry.status = 'uploaded';
      } catch (err) {
        entry.status = 'failed';
        entry.error = err.message || '上传失败';
        renderAttachmentStrip();
        throw new Error(`附件「${entry.file.name}」上传失败：${entry.error}`);
      }
    }
    renderAttachmentStrip();
    return state.attachments.map((entry) => entry.id).filter(Boolean);
  }

  function currentModelName() {
    return els.modelSelect.value || '';
  }

  function updateAttachAvailability() {
    const names = Object.keys(state.modelVision);
    const name = currentModelName();
    const capable = names.filter((key) => state.modelVision[key]);
    const hint = els.attachmentHint;

    if (names.length && state.modelVision[name] !== true) {
      els.attach.disabled = true;
      els.attach.title = '当前模型不支持图片输入';
      hint.hidden = false;
      hint.className = 'composer-hint warn';
      hint.textContent = capable.length
        ? `当前模型 ${name} 不支持图片输入，请切换到 ${capable.join(' / ')}`
        : '当前没有支持图片输入的模型（见 VISION_MODEL_ALIASES）';
      return;
    }

    els.attach.disabled = false;
    els.attach.title = '添加图片';
    hint.hidden = true;
    hint.textContent = '';
  }

  function renderAttachmentThumbs(items) {
    const box = el('div', 'msg-attachments');
    items.forEach((item) => {
      const img = el('img');
      // WHY 允许 src 为空：历史消息的图片要按虚拟路径现取，先占位再回填，
      // 否则得等所有图片取完才渲染（一张慢就把整段历史卡住）。
      if (item.src) img.src = item.src;
      img.alt = item.alt || '附件';
      box.appendChild(img);
    });
    return box;
  }

  function renderUserBubble(content, attachments) {
    const bubble = el('div', 'msg user');
    if (attachments && attachments.length) {
      bubble.appendChild(
        renderAttachmentThumbs(
          attachments.map((entry) => ({ src: entry.url, alt: entry.file.name }))
        )
      );
    }
    if (content) bubble.appendChild(el('div', 'user-text', content));
    els.messages.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  /** 历史消息里的附件按虚拟路径取回内容，逐张回填（失败只影响那一张）。 */
  async function loadHistoryAttachmentImages(container, refs) {
    const images = container.querySelectorAll('.msg-attachments img');
    for (let index = 0; index < refs.length; index += 1) {
      const target = images[index];
      if (!target) continue;
      try {
        const response = await api(
          scoped('/api/workspace/file?path=' + encodeURIComponent(refs[index].path))
        );
        const payload = await response.json();
        if (payload.kind === 'image' && payload.text) {
          target.src = payload.text;
        } else {
          target.alt = `${refs[index].alt || '附件'}（已不可预览）`;
        }
      } catch (err) {
        target.alt = '附件加载失败';
      }
    }
  }

  /* ------------------------------------------------------------------ 交互 */

  async function send() {
    if (state.running) return;
    const content = els.input.value.trim();
    if (!content) return;

    // WHY 在 ensureThread 之前读：它会把 state.threadId 填上，之后就分不清
    // 「这条会话是刚新建的」与「已有会话」了。而这个区分决定了要不要把工作区选择
    // 交给服务端——工作区只在**首条消息**上绑定，后续轮次带上它只会撞 409。
    const drafting = !state.threadId;
    const workspace = drafting ? state.workspaceChoice : null;

    setRunning(true);
    try {
      // WHY 先申请再渲染用户气泡：申请失败时输入内容仍留在输入框里，
      // 用户可以重试，而不是看到一条"发出去了却没被接受"的假消息
      await ensureThread();
      // 把会话写进 URL：此后刷新与分享都能回到这段对话
      navigate(`#/c/${state.threadId}`);

      const hint = els.messages.querySelector('.thread-empty');
      if (hint) hint.remove();

      const editing = state.editTarget;
      // WHY 附件先传再发：上传失败就不该发出这条消息。反过来（先发消息再补图）会
      // 留下一条「发出去了但图没带上」的记录，而那看起来像模型没看懂图。
      // 编辑与重新生成不带附件——它们是从历史检查点分叉，附件已在原消息里。
      let attachmentIds = [];
      let sentAttachments = [];
      if (editing === null && state.attachments.length) {
        attachmentIds = await uploadPendingAttachments(state.threadId, workspace);
        sentAttachments = state.attachments.slice();
      }

      renderUserBubble(content, sentAttachments);
      els.input.value = '';
      els.input.style.height = 'auto';

      const response = await submitRun(
        editing === null
          ? `/api/threads/${state.threadId}/runs`
          : `/api/threads/${state.threadId}/edit`,
        editing === null
          ? {
              content: content,
              model: els.modelSelect.value || null,
              attachment_ids: attachmentIds,
              // 只在新会话的首条消息上带：服务端以它作为这条会话的绑定值
              workspace: workspace,
            }
          : {
              message_index: editing,
              content: content,
              model: els.modelSelect.value || null,
            }
      );

      if (sentAttachments.length) {
        // WHY 不回收这些 object URL：上面那条用户气泡的缩略图正引用着它们。
        // 置空只为清掉输入区的待发送列表。
        state.attachments = [];
        renderAttachmentStrip();
      }

      await handleStream(response);
      // WHY 分叉后重载而不是就地拼接：编辑与重新生成会切到一条**新的**分支，
      // 就地拼出来的画面仍是旧分支的历史加上新回复，看着像接错了上下文。
      // WHY force：此刻仍在 `setRunning(true)` 的区间内，不带 force 会被守卫挡掉。
      if (editing !== null) {
        await openThread(state.threadId, { force: true });
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
      // WHY force：重新生成一定落在一条**新的**分支上，旧画面必须换掉；而这里仍在
      // `setRunning(true)` 区间内，不带 force 会被 openThread 的守卫挡掉。
      await openThread(state.threadId, { force: true });
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
      // 本函数开头已经挡掉运行中的情况，因此这里无需 force：守卫与它同义。
      await openThread(state.threadId);
    } catch (err) {
      appendError('切换分支失败：' + err.message);
    }
  }

  async function loadModels() {
    try {
      const response = await api('/api/models');
      const models = await response.json();
      els.modelSelect.innerHTML = '';
      state.modelVision = {};
      models.forEach((item) => {
        const option = el('option', null, `${item.name} (${item.provider})`);
        option.value = item.name;
        els.modelSelect.appendChild(option);
        state.modelVision[item.name] = item.supports_vision === true;
      });
      // 能力表一变，上传入口的可用性就得跟着变：否则新挂上的纯文本模型仍会显示
      // 「＋」按钮，用户点了才发现不行。
      updateAttachAvailability();
    } catch (err) {
      appendError('模型列表加载失败：' + err.message);
    }
  }

  function bindEvents() {
    els.send.addEventListener('click', send);
    els.stop.addEventListener('click', stopRun);

    els.attach.addEventListener('click', () => els.attachInput.click());
    els.attachInput.addEventListener('change', () => {
      pickFiles(els.attachInput.files);
      // WHY 每次选完清空 input.value：不清的话连续选同一个文件不会再触发 change
      // 事件，「重试」也就无从谈起。
      els.attachInput.value = '';
    });
    els.modelSelect.addEventListener('change', updateAttachAvailability);

    // 拖拽落点覆盖消息区与输入区：只认其中一个会让「拖到对话框上」变成浏览器
    // 直接打开该文件。必须 preventDefault 才能接管这个默认行为。
    [els.messages, els.attachmentStrip].forEach((target) => {
      target.addEventListener('dragover', (event) => event.preventDefault());
      target.addEventListener('drop', (event) => {
        event.preventDefault();
        if (els.attach.disabled) return;
        pickFiles(event.dataTransfer && event.dataTransfer.files);
      });
    });

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

  /* ------------------------------------------------------------------ 审计日志面板 */

  function openAuditModal() {
    els.auditModal.style.display = '';
    loadAudit();
  }

  function closeAuditModal() {
    els.auditModal.style.display = 'none';
  }

  async function loadAudit() {
    try {
      const response = await api('/api/audit?limit=100');
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

  function bindAuditEvents() {
    els.auditOpen.addEventListener('click', openAuditModal);
    els.auditClose.addEventListener('click', closeAuditModal);
    els.auditRefresh.addEventListener('click', () => loadAudit());
    // 点击遮罩关闭弹窗
    els.auditModal.querySelector('.modal-backdrop').addEventListener('click', closeAuditModal);
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

  /* ------------------------------------------------------------------ 知识库面板 */

  function openKnowledgeModal() {
    els.knowledgeModal.style.display = '';
    loadKnowledge();
  }

  function closeKnowledgeModal() {
    els.knowledgeModal.style.display = 'none';
  }

  async function loadKnowledge() {
    els.knowledgeList.innerHTML = '';
    els.knowledgeList.appendChild(el('div', 'knowledge-empty', '加载中…'));
    try {
      const response = await api(scoped('/api/knowledge'));
      state.knowledge = await response.json();
      renderKnowledge();
    } catch (err) {
      state.knowledge = null;
      els.knowledgeCaps.innerHTML = '';
      els.knowledgeList.innerHTML = '';
      els.knowledgeList.appendChild(el('div', 'knowledge-empty', `加载失败：${err.message}`));
    }
  }

  function renderKnowledge() {
    const payload = state.knowledge;
    if (!payload) return;
    const caps = payload.capabilities || {};
    const stats = payload.stats || {};

    // 能力条：把「这次到底走不走语义」摆出来。面板若只显示文档清单，用户会以为
    // 检索一直是语义的——而 EMBEDDING_BACKEND=none 时它只是关键词匹配。
    els.knowledgeCaps.innerHTML = '';
    els.knowledgeCaps.appendChild(
      el(
        'span',
        'knowledge-chip',
        caps.vector_enabled
          ? `语义检索：${caps.embedding_backend || '已启用'}`
          : '语义检索：未启用（仅关键词）'
      )
    );
    els.knowledgeCaps.appendChild(
      el('span', 'knowledge-chip', `分块 ${caps.chunk_chars} 字 · 重叠 ${caps.chunk_overlap_chars} 字`)
    );
    els.knowledgeCaps.appendChild(el('span', 'knowledge-chip', `检索返回 ${caps.top_k} 条`));

    els.knowledgeList.innerHTML = '';
    els.knowledgeList.appendChild(
      el(
        'div',
        'knowledge-stats',
        `已索引 ${stats.document_count || 0} 份文档 · ${stats.chunk_count || 0} 个分块 · ${stats.vector_count || 0} 条向量`
      )
    );

    const items = payload.items || [];
    if (!items.length) {
      els.knowledgeList.appendChild(
        el(
          'div',
          'knowledge-empty',
          '还没有索引任何文档。点「索引工作区」把工作区里的 Markdown、笔记与代码注释收进来。'
        )
      );
      return;
    }

    items.forEach((item) => {
      const row = el('div', 'knowledge-row');
      const main = el('div', 'knowledge-main');
      main.appendChild(el('div', 'knowledge-path', item.source_path));
      main.appendChild(
        el('div', 'knowledge-sub', `${item.chunk_count} 个分块 · ${formatTime(item.indexed_at)}`)
      );
      row.appendChild(main);

      const remove = el('button', 'danger small', '移除');
      remove.type = 'button';
      remove.addEventListener('click', () => removeKnowledge(item));
      row.appendChild(remove);
      els.knowledgeList.appendChild(row);
    });
  }

  async function indexKnowledge() {
    // WHY 在请求期间禁用按钮：一次整区索引可能要调用几百次嵌入，重复点击会并发起
    // 多份相同的工作，而它们之间没有任何互斥。
    els.knowledgeIndex.disabled = true;
    try {
      const response = await api(scoped('/api/knowledge'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const summary = await response.json();
      // WHY 把「跳过」也报出来：索引 3 个与跳过 3 个是完全不同的结果，只报成功的
      // 数字会让用户以为全都进去了，之后检索不到又无从解释。
      const parts = [
        `扫描 ${summary.scanned} 个文件`,
        `新索引 ${summary.indexed} 个`,
        `未变化 ${summary.unchanged} 个`,
      ];
      if (summary.empty) parts.push(`无可索引内容 ${summary.empty} 个`);
      if (summary.skipped) parts.push(`跳过 ${summary.skipped} 个`);
      window.alert(`${parts.join('，')}。`);
      await loadKnowledge();
    } catch (err) {
      window.alert(`索引失败：${err.message}`);
    } finally {
      els.knowledgeIndex.disabled = false;
    }
  }

  async function removeKnowledge(item) {
    // 二次确认与记忆面板同一理由：移除后 Agent 就检索不到这份文档了，而这一步不可撤销。
    if (!window.confirm(`从知识库移除这份文档？\n${item.source_path}\n\n工作区里的源文件不会被删除。`)) {
      return;
    }
    try {
      // WHY 用查询参数而不是路径段：虚拟路径本身含 `/`，放进路径段要靠百分号编码
      // 才能传对，而那正是最容易写错、且在日志里最难辨认的写法。
      await api(scoped(`/api/knowledge?path=${encodeURIComponent(item.source_path)}`), {
      method: 'DELETE',
    });
      await loadKnowledge();
    } catch (err) {
      window.alert(`移除失败：${err.message}`);
    }
  }

  function bindKnowledgeEvents() {
    els.knowledgeOpen.addEventListener('click', openKnowledgeModal);
    els.knowledgeClose.addEventListener('click', closeKnowledgeModal);
    els.knowledgeRefresh.addEventListener('click', () => loadKnowledge());
    els.knowledgeIndex.addEventListener('click', indexKnowledge);
    els.knowledgeModal.querySelector('.modal-backdrop').addEventListener('click', closeKnowledgeModal);
  }

  /* ------------------------------------------------------------------ 技能库面板 */

  function openSkillsModal() {
    els.skillsModal.style.display = '';
    loadSkills();
  }

  function closeSkillsModal() {
    els.skillsModal.style.display = 'none';
  }

  async function loadSkills() {
    els.skillsList.innerHTML = '';
    els.skillsList.appendChild(el('div', 'knowledge-empty', '加载中…'));
    try {
      const response = await api(scoped('/api/skills'));
      state.skills = await response.json();
      renderSkills();
    } catch (err) {
      state.skills = null;
      els.skillsCaps.innerHTML = '';
      els.skillsList.innerHTML = '';
      els.skillsList.appendChild(el('div', 'knowledge-empty', `加载失败：${err.message}`));
    }
  }

  function renderSkills() {
    const payload = state.skills;
    if (!payload) return;

    els.skillsCaps.innerHTML = '';
    els.skillsCaps.appendChild(el('span', 'knowledge-chip', `作用域 ${payload.scope || 'global'}`));
    els.skillsCaps.appendChild(
      el(
        'span',
        'knowledge-chip',
        payload.graph_sources && payload.graph_sources.length
          ? `来源 ${payload.graph_sources.join(' ')}`
          : '来源：无'
      )
    );
    els.skillsList.innerHTML = '';

    // 视图告警单独占一行、且排在清单之前：它一出现，「启停当前不生效」就是当下最该知道
    // 的事，放到列表后面会被一屏技能名淹没。
    if (payload.view_warning) {
      els.skillsList.appendChild(el('div', 'knowledge-empty', payload.view_warning));
    }

    const items = payload.items || [];
    if (!items.length && !payload.view_warning) {
      els.skillsList.appendChild(
        el(
          'div',
          'knowledge-empty',
          '还没有技能。把技能包放进工作区的 skills/ 目录（每个包一个目录、内含 SKILL.md），再点刷新。'
        )
      );
    }

    items.forEach((item) => {
      const row = el('div', 'knowledge-row');
      const main = el('div', 'knowledge-main');
      main.appendChild(el('div', 'knowledge-path', item.name));
      if (item.description) {
        main.appendChild(el('div', 'knowledge-sub', item.description));
      }
      // 上游只告警的问题要显出来：它意味着这个技能形态可疑（例如目录名与 name 不符）。
      // 不显的话，用户只会看到「技能在，但好像没起作用」，没有任何线索。
      (item.problems || []).forEach((problem) => {
        main.appendChild(el('div', 'knowledge-sub', `注意：${problem}`));
      });
      if (!item.enabled) {
        main.appendChild(el('div', 'knowledge-sub', '已停用，Agent 不会加载它'));
      }
      row.appendChild(main);

      const toggle = el(
        'button',
        item.enabled ? 'small' : 'primary small',
        item.enabled ? '停用' : '启用'
      );
      toggle.type = 'button';
      toggle.addEventListener('click', () => toggleSkill(item));
      row.appendChild(toggle);
      els.skillsList.appendChild(row);
    });

    // 没能加载的候选目录：上游对它们只写日志。不在这里列出，用户看到的就是
    // 「我明明建了它，面板里却没有」，且没有任何可查的线索。
    (payload.unloadable || []).forEach((item) => {
      const row = el('div', 'knowledge-row');
      const main = el('div', 'knowledge-main');
      main.appendChild(el('div', 'knowledge-path', item.directory));
      main.appendChild(el('div', 'knowledge-sub', `未能加载：${item.reason}`));
      row.appendChild(main);
      els.skillsList.appendChild(row);
    });

    (payload.load_errors || []).forEach((reason) => {
      els.skillsList.appendChild(el('div', 'knowledge-empty', `来源目录读取失败：${reason}`));
    });
  }

  async function toggleSkill(item) {
    const next = !item.enabled;
    // WHY 停用要二次确认、启用不要：技能集是全体共享的一份，停用会让**所有人**的 Agent
    // 少一项能力，而启用只是把它还回来。风险不对称，确认也就不该对称。
    if (
      !next &&
      !window.confirm(
        `停用技能「${item.name}」？\n\n技能集是全体共享的，停用后所有人的 Agent 都不再加载它。\n（已在进行的会话不受影响，新建会话才生效。）`
      )
    ) {
      return;
    }
    try {
      await api(scoped(`/api/skills/${encodeURIComponent(item.name)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      await loadSkills();
    } catch (err) {
      window.alert(`操作失败：${err.message}`);
    }
  }

  function bindSkillsEvents() {
    els.skillsOpen.addEventListener('click', openSkillsModal);
    els.skillsClose.addEventListener('click', closeSkillsModal);
    els.skillsRefresh.addEventListener('click', () => loadSkills());
    els.skillsModal.querySelector('.modal-backdrop').addEventListener('click', closeSkillsModal);
  }

  /* ------------------------------------------------------------------ 工作区面板 */

function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * 渲染这条会话的文件根（路径 + 它属于哪一类）。
 *
 * WHY 每次打开面板都重取而不是首屏取一次：根是**会话级**状态（用户选的项目，或应用为
 * 这条会话建的专属目录），而页面可以比会话活得久——缓存一次会让面板显示一个已经不再
 * 生效的目录，而「我到底在改哪里」正是这个面板要回答的第一个问题。
 *
 * WHY 要区分「工作空间」与「会话专属目录」：两者的路径形态可能很像，但含义完全不同——
 * 前者是你的项目目录（多条会话可共用），后者只有这条会话在用。混在一起说，用户会以为
 * 自己一直在改自己的项目。
 */
async function loadWorkspaceInfo() {
  const generation = state.workspace.generation;
  try {
    const response = await api(scoped('/api/workspace/info'));
    const payload = await response.json();
    // 期间换过会话：这份结果说的是**上一条**会话的根，写上去会让顶栏提示与事实相反。
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
    const label = payload.bound ? '工作空间' : '会话专属目录';
    // 锁定是用户最需要知道的一条事实：它解释了「为什么换不了」。
    const lockNote = payload.locked ? '（已锁定：本条会话不再支持更换）' : '';
    els.workspacePath.textContent = payload.path
      ? `${label}：${payload.path}${lockNote}`
      : '';
    els.workspacePath.title = payload.path || '';
    // 顶栏入口也带上路径：面板不打开时，悬停是确认「现在在改哪个目录」最省事的路径。
    els.workspaceOpen.title = payload.path ? `${label}：${payload.path}` : '工作区';
  } catch (err) {
    // 期间换过会话：这条失败说的是上一条会话的根，写上去会让用户以为**当前**这条读取失败。
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
    // 409 的两种含义都是**正常形态**而不是故障：草稿态且没选工作空间（还没有根）、
    // 或这条会话选定的目录不见了。把它们统一说成「读取失败」会让用户去查网络，
    // 而该做的是选一个工作空间 / 把那个目录恢复回来——服务端给的文案里已经写明了。
    const text = err.status === 409 ? err.message : `文件根读取失败：${err.message}`;
    // WHY 降级为一行提示而不是弹错：路径显示失败不该挡住目录树本身——
    // 面板的主要用途（看 Agent 产出了什么）仍然可用。
    els.workspacePath.textContent = text;
    els.workspacePath.title = '';
    els.workspaceOpen.title = `工作区（${text}）`;
  }
}

/**
 * 把工作区相关的界面切到**当前会话**的根上。
 *
 * WHY 换会话时必须调用它：根是会话级状态，而这里的界面状态有四份——面板里的路径文案、
 * 顶栏悬停提示、按虚拟路径缓存的目录树、以及文件预览。``dirs`` 是按**虚拟路径**缓存的
 * （``/``、``/src``），而服务端对每个根都从 ``/`` 开始编号：于是切到 B 之后，
 * ``openWorkspaceModal`` 会因为 ``dirs['/']`` 命中缓存而**不再请求**、直接重画，
 * 面板就变成「左上角写着 B 的路径、右边列着 A 的文件」，全程不报错。
 *
 * WHY 收成一个函数而不是在切换的两处各写一遍：切换有两条路径（打开既有会话、
 * 退回草稿态），而这份清理漏掉任何一处，都只有在特定顺序下点界面才会暴露。
 */
function syncWorkspaceScope() {
  // 先换作用域再发请求：在途的旧请求回来时会发现自己的代号过期，自己丢弃结果。
  state.workspace = WorkspaceScope.begin(state.workspace);
  clearWorkspacePreview();
  loadWorkspaceInfo();
  // 面板没开就不发目录请求：它下次打开时会自己取（``openWorkspaceModal``）。
  if (els.workspaceModal.style.display !== 'none') loadDirectory('/');
}

/**
 * 清空文件预览。
 *
 * WHY 换根时必须清：预览里是**上一个根**里的文件内容，留着它等于在 B 会话里显示
 * A 的文件正文——而左侧的树已经是 B 的，用户会把那当成「这条会话里也有这个文件」。
 * 草稿态没有根时留一个说明，比一个空白框更容易理解。
 */
function clearWorkspacePreview() {
  els.workspacePreview.innerHTML = '';
  els.workspacePreview.appendChild(
    el('div', 'workspace-empty', '从左侧选择一个文件查看内容')
  );
}

/**
 * 渲染「本次会话工作空间」选择器。
 *
 * WHY 只在草稿态出现：文件根在**第一条交互**上锁定，之后不可变更（换项目要新建会话）。
 * 会话已存在时继续显示一个改不动的控件，只会让用户以为「选一下就能切」——而服务端会以
 * 409 拒绝，那个报错看起来像 bug 而不是设计。
 *
 * WHY 不选也是一种显式状态、且必须写出来：不绑定时这条会话落在应用为它建的专属目录里，
 * 而不是某个项目目录。不写出来的话，用户会默认「没选 = 用默认项目」——然后在自己的项目
 * 里找不到 Agent 刚产出的文件。
 */
function renderWorkspacePicker() {
  const drafting = !state.threadId;
  els.workspacePicker.hidden = !drafting;
  if (!drafting) return;

  const chosen = state.workspaceChoice;
  els.workspaceChoiceText.textContent = chosen
    ? chosen
    : '未选择 —— 本条会话将使用应用为它创建的专属目录';
  els.workspaceChoiceText.title = chosen || '';
  // 「不绑定」按钮只在已选时可用：没选的时候它什么也不做，留着反而像个必须点的步骤。
  els.workspaceClear.disabled = !chosen;
}

/* ------------------------------------------------------------------ 工作区浏览 */

/**
 * 拉取并渲染浏览弹窗的一层目录。
 *
 * WHY 只拉一层、点一次拉一次：服务端才是边界的所有者（目录也可能随时被删），客户端
 * 缓存整棵树只会造出第二份会过时的事实。层数也不多——用户点几下就到目标目录了。
 */
async function loadWorkspaceBrowse(path) {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const response = await api(`/api/workspaces/dirs${query}`);
  state.workspaceBrowse = await response.json();
  renderWorkspaceBrowse();
}

function renderWorkspaceBrowse() {
  const view = state.workspaceBrowse;
  const atStart = !view.path;
  els.workspaceBrowsePath.textContent = atStart ? '选择一个位置' : view.path;
  // 输入框跟随当前目录：它既是「粘一个新路径」的入口，也是「我现在在哪」的显示。
  // 不同步的话，进到下一层后框里还留着上一个路径，用户按回车又跳回去。
  els.workspaceBrowseInput.value = view.path || '';
  // 到文件系统顶层（盘符根或 /）时服务端给 parent=null：那里没有「上一级」，
  // 按钮该是禁用的（点了也只会原地不动）。
  els.workspaceBrowseUp.disabled = !view.parent;
  // 起点页（还没进到任何目录里）不能「使用此目录」——那时没有「此目录」这回事
  els.workspaceBrowseUse.disabled = atStart;

  els.workspaceBrowseList.replaceChildren();
  if (!view.entries.length) {
    els.workspaceBrowseList.appendChild(
      el('p', 'workspace-browse-empty', atStart ? '没有可浏览的位置' : '这里没有子目录')
    );
    return;
  }
  view.entries.forEach((entry) => {
    const row = el('button', 'workspace-browse-item', entry.name);
    row.type = 'button';
    row.title = entry.path;
    row.addEventListener('click', () => {
      loadWorkspaceBrowse(entry.path).catch((err) => {
        els.workspaceBrowseList.replaceChildren(
          el('p', 'workspace-browse-empty', `读取失败：${err.message}`)
        );
      });
    });
    els.workspaceBrowseList.appendChild(row);
  });
}

async function openWorkspaceBrowse() {
  els.workspaceBrowseModal.style.display = '';
  try {
    // 不传 path 表示「起点」：服务端给出顶层（Windows 是盘符列表，POSIX 是 /）
    await loadWorkspaceBrowse(null);
  } catch (err) {
    els.workspaceBrowseList.replaceChildren(
      el('p', 'workspace-browse-empty', `无法浏览目录：${err.message}`)
    );
  }
}

function closeWorkspaceBrowse() {
  els.workspaceBrowseModal.style.display = 'none';
}

/**
 * 跳到输入框里那个路径。
 *
 * WHY 走浏览端点而不是单独加一个「校验路径」接口：那条接口要处理「存在吗 / 允许吗 /
 * 是目录吗」三件事，而浏览端点本来就要回答同样三个问题。复用它，等于保证「跳得进去的
 * 位置」与「选得中的位置」永远是同一个集合——这两个集合一旦分叉，用户会看到「路径明明
 * 能打开、却选不了」。
 */
function goToTypedPath() {
  const value = els.workspaceBrowseInput.value.trim();
  if (!value) return;
  loadWorkspaceBrowse(value).catch((err) => {
    // 失败原因直接显示在列表区：那是用户刚看一眼的地方，弹 toast 反而会让他找不到
    // 「路径到底哪里不对」的上下文。
    els.workspaceBrowseList.replaceChildren(el('p', 'workspace-browse-empty', err.message));
  });
}

/**
 * 把浏览到的当前目录定为这条会话的工作空间。
 *
 * WHY 只写本地状态、不立刻发给服务端：文件根在**首条消息**上锁定（会话在那一刻才真正
 * 诞生）。提前提交只会造出一条「有根但没有内容」的记录。
 */
function useBrowsedWorkspace() {
  const chosen = state.workspaceBrowse.path;
  if (!chosen) return;
  applyWorkspaceChoice(chosen);
  closeWorkspaceBrowse();
}

/**
 * 记下选择（或清除选择），并把界面与已打开的面板同步到它。
 *
 * WHY 收成一个函数：选择有三个入口（系统弹窗、网页内浏览、「不绑定」），三处各写一遍
 * 必然会漏掉「刷新已打开的面板」这一步——而那一步漏掉的表现是「面板还显示上一个目录
 * 的文件」，用户会把那当成「新工作空间里居然有旧文件」。
 */
function applyWorkspaceChoice(chosen) {
  state.workspaceChoice = chosen || null;
  renderWorkspacePicker();
  setWorkspacePickStatus('');
  // WHY 复用「换根」那一份逻辑（而不是只清目录树）：选了工作空间同样是换了根，而
  // 「换根要丢哪些缓存、在途请求怎么作废」的规则只有一份——写第二份就会漂开，
  // 而漂开的表现是「某种换根方式下面板还列着上一个目录的文件」。
  syncWorkspaceScope();
}

function openWorkspaceModal() {
  els.workspaceModal.style.display = '';
  loadWorkspaceInfo();
  if (!state.workspace.dirs['/']) {
    loadDirectory('/');
  } else {
    renderTree();
  }
}

function closeWorkspaceModal() {
  els.workspaceModal.style.display = 'none';
}

/**
 * 打开**服务端**的系统文件夹选择弹窗。
 *
 * WHY 要提示「窗口在服务端那台机器上」：这个对话框出现在运行服务端的那台机器的屏幕上
 * （浏览器拿不到宿主的绝对路径，只能由服务端进程自己弹）。不说清楚，用户在远程或容器
 * 部署里会一直盯着浏览器等一个永远不会出现在这里的窗口。
 */
async function pickWorkspaceFolder() {
  if (state.workspacePickPending) return;
  state.workspacePickPending = true;
  els.workspacePick.disabled = true;
  setWorkspacePickStatus('请在系统对话框里选择目录…（窗口出现在运行服务端的那台机器上）');
  try {
    const chosen = state.workspaceChoice
      ? `?workspace=${encodeURIComponent(state.workspaceChoice)}`
      : '';
    const response = await api(`/api/workspaces/pick${chosen}`, { method: 'POST' });
    const payload = await response.json();
    if (payload.cancelled) {
      // 取消不是错误：清掉提示即可，不弹任何红条
      setWorkspacePickStatus('');
      return;
    }
    applyWorkspaceChoice(payload.path);
  } catch (err) {
    // WHY 在提示里直接给出替代路径：最常见的失败是「这个部署没有图形环境」（501），
    // 那时用户需要知道的不是错误码，而是「改用哪个按钮」。
    setWorkspacePickStatus(`${err.message}；可改用「浏览…」在网页里逐级挑选。`);
  } finally {
    state.workspacePickPending = false;
    els.workspacePick.disabled = false;
  }
}

function setWorkspacePickStatus(text) {
  els.workspacePickStatus.textContent = text || '';
  els.workspacePickStatus.hidden = !text;
}

async function loadDirectory(path) {
  // 记下这次请求属于哪个根作用域：面板开着时换会话，旧请求可能在新请求之后返回，
  // 而它带回来的是**上一个根**的目录内容（虚拟路径同名却指向别处，所以连报错都没有）。
  const generation = state.workspace.generation;
  try {
    // WHY 编码整个 path 作为一个查询参数：虚拟路径里可能含中文与空格，
    // 而这里它是 query value，不是路径段——整体编码才是正确做法。
    const response = await api(scoped(`/api/workspace/files?path=${encodeURIComponent(path)}`));
    const payload = await response.json();
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
    state.workspace.dirs[payload.path] = {
      entries: payload.entries || [],
      truncated: !!payload.truncated,
    };
    renderTree();
  } catch (err) {
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
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
  const generation = state.workspace.generation;
  state.workspace.selected = path;
  // 展开并加载父目录，让用户能看出「我打开的这份文件在哪」
  const parent = path.split('/').slice(0, -1).join('/') || '/';
  state.workspace.expanded[parent] = true;
  if (!state.workspace.dirs[parent]) await loadDirectory(parent);
  // 上面那次 ``await`` 期间可能已经换了会话：树的缓存已被整份丢掉，此时再画一遍
  // 只是画空（无害），但下面的预览必须先确认自己还在同一个根上。
  if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
  renderTree();

  els.workspacePreview.innerHTML = '';
  els.workspacePreview.appendChild(el('div', 'workspace-empty', '加载中…'));
  try {
    const response = await api(scoped(`/api/workspace/file?path=${encodeURIComponent(path)}`));
    const payload = await response.json();
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
    renderFile(payload);
  } catch (err) {
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
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
  const generation = state.workspace.generation;
  button.disabled = true;
  button.textContent = '加载中…';
  try {
    const response = await api(
      scoped(`/api/workspace/file?path=${encodeURIComponent(cursor.path)}&offset=${cursor.offset}`)
    );
    const payload = await response.json();
    // 期间换过会话：预览区已被换成新根的内容，这份响应属于上一个根——而这条分支会
    // 往**活的**预览区里追加「已显示全部 N 字符」，那正是最难被解释成「上一条会话的
    // 残留」的东西（它看起来像新文件自己的一行）。
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
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
    if (!WorkspaceScope.isCurrent(state.workspace, generation)) return;
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

  els.workspaceBrowseOpen.addEventListener('click', openWorkspaceBrowse);
  els.workspaceBrowseClose.addEventListener('click', closeWorkspaceBrowse);
  els.workspaceBrowseModal
    .querySelector('.modal-backdrop')
    .addEventListener('click', closeWorkspaceBrowse);
  els.workspaceBrowseUp.addEventListener('click', () => {
    // parent 为空说明已到达浏览根（服务端不再给上级），此时按钮是禁用的
    if (!state.workspaceBrowse.parent) return;
    loadWorkspaceBrowse(state.workspaceBrowse.parent).catch((err) => {
      els.workspaceBrowseList.replaceChildren(
        el('p', 'workspace-browse-empty', `读取失败：${err.message}`)
      );
    });
  });
  els.workspaceBrowseUse.addEventListener('click', useBrowsedWorkspace);
  els.workspaceBrowseGo.addEventListener('click', goToTypedPath);
  els.workspaceBrowseInput.addEventListener('keydown', (event) => {
    // Enter 直接前往：粘贴路径之后按回车是最自然的动作
    if (event.key !== 'Enter') return;
    event.preventDefault();
    goToTypedPath();
  });
  // 选中的工作空间只存在本地，直到首条消息发出时才交给服务端（锁定发生在那时）。
  els.workspacePick.addEventListener('click', pickWorkspaceFolder);
  // 「不绑定」：把选择清成 null。它不是「取消」而是另一种正常结果——这条会话将使用应用
  // 为它创建的专属目录。
  els.workspaceClear.addEventListener('click', () => applyWorkspaceChoice(null));
}

  async function init() {
    bindEvents();
    bindAuditEvents();
    bindMemoryEvents();
    bindWorkspaceEvents();
    bindKnowledgeEvents();
    bindSkillsEvents();

    // WHY 只注册不直接调用：navigate() 赋值 hash 同样会触发该事件，
    // 让「URL 变化 → 同步界面」成为唯一入口，避免两处逻辑漂移
    window.addEventListener('hashchange', () => {
      syncWithUrl();
    });

    await loadModels();
    // 上限在模型之后加载：两者都只影响输入区的可用性，而模型决定了「能不能传」
    await loadAttachmentLimits();
    await loadThreads();
    // 刷新时按 URL 恢复：带会话 ID 则拉历史，否则进入草稿态（不创建任何东西）
    await syncWithUrl();
    els.input.focus();
  }

  init();
})();
