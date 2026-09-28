/**
 * 把助手消息里的 ```mermaid 代码块渲染成图。
 *
 * 与 markdown.js 的分工：markdown.js 只负责「把源码变成 `<pre class="md-code">`」，
 * 图是它的下游——所以这里做的是**后处理**：拿到已渲染的容器，找出里面的 mermaid
 * 代码块，就地换成图。这样两条渲染路径（历史、流式收尾）共用同一段代码。
 *
 * 四条不能让步的取舍：
 *
 * 1. **按需加载第三方发行文件**。`vendor/mermaid.min.js` 有 2.7 MB，而绝大多数会话里
 *    一张图都没有——写进 `index.html` 等于让每次打开页面都付一次解析代价。真正需要它
 *    的那一刻（第一次扫到 mermaid 代码块）才注入 `<script>`，且全局只注入一次。
 *
 * 2. **失败必须降级、且绝不吞掉源码**。渲染不出来时把代码块**原样留在原处**，只在它
 *    前面加一行原因。理由：源码是用户唯一还能用的东西（复制到别处照样能渲染），而失败
 *    本身并不说明这张图是错的——它可能是我们这侧的资源缺失、版本过旧或语法支持差异。
 *    把原文一起抹掉，用户会以为模型什么都没输出。
 *
 * 3. **图源码是不可信输入**。它来自模型，而 mermaid 默认会把标签文本当 HTML 处理。
 *    因此 `securityLevel: 'strict'`（插入前过一遍内置的 DOMPurify），并且**不做**
 *    任何「先塞进 innerHTML 再挑出 svg」的动作——解析结果先待在游离文档里，
 *    确认是 svg 之后才挂进页面。
 *
 * 4. **渲染是幂等且串行的**。幂等：每个代码块打一个状态标记，历史路径与流式路径即使
 *    扫到同一块也只渲染一次。串行：mermaid 的 `render` 会写它自己的全局状态（临时容器、
 *    主题变量、id 计数），并发调用会互相踩，表现是「偶发地渲染出空白或别人的图」，
 *    而它并不报错。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MermaidView = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /** markdown.js 为 ```mermaid 生成的 class 后缀；识别时按 token 比对，不做前缀匹配。 */
  const LANGUAGE = 'mermaid';

  /** 内置发行文件的位置（同源静态资源，见 vendor/README.md）。 */
  const VENDOR_URL = '/vendor/mermaid.min.js';

  /** 代码块上的处理状态：pending / rendering / done / failed / detached。 */
  const STATE_ATTR = 'data-mermaid-state';

  /**
   * 单张图的源码长度上限。
   *
   * WHY 需要这道闸：布局是同步计算，几十 KB 的源码会把主线程按在那里几秒（界面完全
   * 无响应，看起来像卡死），而那种规模的图人也读不了。超过上限时走降级、保留源码。
   */
  const MAX_SOURCE_CHARS = 20000;

  /** 只认渲染器自己生成的代码块，不碰工具输出与工作区预览里的 `<pre>`。 */
  const SELECTOR = 'pre.md-code code';

  /** 失败原因的展示上限：mermaid 的语法错误带行号与期望 token，整段会淹没正文。 */
  const MAX_REASON_CHARS = 300;

  /** 库加载的进行中承诺；失败后清空以便下次重试（见 loadLibrary）。 */
  let libraryPromise = null;

  /** 一次性配置是否已做完。 */
  let configured = false;

  /** 图 id 的全局计数：同一次会话里多张图必须拿到互不相同的 id。 */
  let sequence = 0;

  /**
   * 判断一个 `<code>` 是不是 Mermaid 代码块。
   *
   * WHY 按 class token 而不是选择器 `code.language-mermaid`：class 选择器在 HTML 文档里
   * 区分大小写，而模型输出 ```Mermaid 并不罕见；解析 token 才能把它一并接住。
   *
   * @param {Element} element 候选节点。
   * @returns {boolean}
   */
  function isMermaidCode(element) {
    if (!element || typeof element.getAttribute !== 'function') return false;
    const classAttribute = element.getAttribute('class');
    if (!classAttribute) return false;
    const tokens = String(classAttribute).split(' ');
    for (let index = 0; index < tokens.length; index += 1) {
      if (tokens[index].trim().toLowerCase() === 'language-' + LANGUAGE) return true;
    }
    return false;
  }

  /**
   * 取出一块代码里的图源码，并判定它是否值得送进渲染器。
   *
   * WHY 从 `textContent` 取而不是 `innerHTML`：渲染器已把源码整段转义，重新读出实体
   * （`&lt;` → `<`）才是模型原本写的那些字符；拿 HTML 去渲染等于把转义面又还回去。
   *
   * @param {Element} element `<code>` 节点。
   * @returns {{ok: boolean, source: string, reason: string}} 不可渲染时 `reason` 说明原因。
   */
  function diagramSource(element) {
    const raw = element && typeof element.textContent === 'string' ? element.textContent : '';
    const source = raw.trim();
    if (!source) return { ok: false, source: '', reason: '图源码为空' };
    if (source.length > MAX_SOURCE_CHARS) {
      return {
        ok: false,
        source: source,
        reason: '图源码过长（' + source.length + ' 字，上限 ' + MAX_SOURCE_CHARS + ' 字）',
      };
    }
    return { ok: true, source: source, reason: '' };
  }

  /** 一次性配置主题与安全级别。 */
  function configure(mermaid) {
    if (configured) return;
    mermaid.initialize({
      // 绘制时机由「消息出现」驱动：交给它自己扫全页会与流式渲染抢同一块节点，
      // 且每次重绘都要把整页再扫一遍。
      startOnLoad: false,
      // 图源码来自模型，标签文本会被当 HTML 处理——strict 让它在插入前过一遍净化。
      securityLevel: 'strict',
      // 它默认会往页面里塞一张自带的错误图，与我们下面「保留源码 + 说明原因」的降级
      // 重复，而且那张图的样式不受本页控制。
      suppressErrorRendering: true,
      theme: 'default',
      fontFamily: 'inherit',
    });
    configured = true;
  }

  /**
   * 加载内置的 Mermaid 发行文件（全局只注入一次 `<script>`）。
   *
   * @returns {Promise<object>} 浏览器全局 `mermaid`。
   */
  function loadLibrary() {
    if (libraryPromise) return libraryPromise;

    libraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = VENDOR_URL;
      script.async = true;
      script.addEventListener('load', () => {
        if (!window.mermaid) {
          reject(new Error(VENDOR_URL + ' 已加载，但没有导出全局 mermaid'));
          return;
        }
        configure(window.mermaid);
        resolve(window.mermaid);
      });
      script.addEventListener('error', () => {
        reject(new Error('无法加载 ' + VENDOR_URL + '（文件缺失或不可读）'));
      });
      document.head.appendChild(script);
    });

    // WHY 失败不缓存：把一个失败的承诺留着，等于「文件补回来之后整页再也拉不起来」，
    // 用户唯一的补救动作变成刷新页面——那是最不该被逼出来的一步。
    libraryPromise.catch(() => {
      libraryPromise = null;
    });
    return libraryPromise;
  }

  /**
   * 收集容器里尚未处理的 Mermaid 代码块，并**立即**打上标记。
   *
   * WHY 在收集时就标记而不是开始渲染时才标记：历史渲染与流式收尾是两条彼此独立的
   * 路径，它们可能在同一帧里都扫到同一块——等到「开始渲染」才标记，就等于允许两次入队，
   * 而第二次入队的结果是把同一个位置插入两张图。
   *
   * @param {Element} container 助手气泡或它的正文容器。
   * @returns {Array<object>} 待处理的块（可能为空）。
   */
  function collect(container) {
    const blocks = [];
    const candidates = container.querySelectorAll(SELECTOR);
    Array.prototype.forEach.call(candidates, (code) => {
      if (!isMermaidCode(code)) return;
      if (code.getAttribute(STATE_ATTR)) return;
      const parsed = diagramSource(code);
      code.setAttribute(STATE_ATTR, 'pending');
      blocks.push({
        code: code,
        // 代码块本身是替换/降级时的锚点；它缺席时退回 <code>，保证内容不会被丢在半路。
        anchor: code.parentElement || code,
        ok: parsed.ok,
        source: parsed.source,
        reason: parsed.reason,
      });
    });
    return blocks;
  }

  /** 渲染结果的截断：错误消息可能很长，但界面要能读。 */
  function describeError(err) {
    const message = err && err.message ? String(err.message) : String(err);
    if (message.length <= MAX_REASON_CHARS) return message;
    return message.slice(0, MAX_REASON_CHARS) + '…';
  }

  /**
   * 清掉 mermaid 失败时可能留在 `<body>` 里的临时容器。
   *
   * WHY 需要我们自己擦：`render` 会在 body 里挂一个 `#d{id}` 的临时节点做度量，
   * 成功路径上它会自己移除，而抛错路径上并不保证——残留节点是空 div，不报错，
   * 但会随每次失败累积（例如用户反复重跑同一轮）。
   */
  function removeLeftover(renderId) {
    const leftover = document.getElementById('d' + renderId);
    if (leftover && leftover.parentElement) leftover.parentElement.removeChild(leftover);
  }

  /**
   * 从 mermaid 给出的标记里取出真正的 `<svg>` 节点。
   *
   * WHY 走 `DOMParser` 而不是把标记塞进一个游离 div 的 innerHTML：`DOMParser` 得到的是
   * **惰性文档**，里面的 `<img>` 之类不会去取资源；而 innerHTML 即便节点游离也可能触发
   * 资源加载。这里要的只是「确认它是 svg」，不该附带任何取资源的副作用。
   *
   * @param {string} markup mermaid 的返回值 `result.svg`。
   * @returns {Element|null} 可安全插入的 svg 节点；不是 svg 时返回 null。
   */
  function extractSvg(markup) {
    if (typeof markup !== 'string' || !markup) return null;
    const parsed = new DOMParser().parseFromString(markup, 'text/html');
    const svg = parsed.body.querySelector('svg');
    if (!svg) return null;
    return document.importNode(svg, true);
  }

  /**
   * 用图替换掉原来的代码块，并把源码留在可展开的细节里。
   *
   * WHY 保留源码：渲染是一次「尽力而为」的转译（换主题、换字体、换版本都可能不一样），
   * 用户要核对或复制原图时不该被迫回到上一轮对话里翻。
   *
   * @param {object} block `collect` 产出的一项。
   * @param {Element} svg 已解析好的 svg 节点。
   */
  function replaceWithDiagram(block, svg) {
    const anchor = block.anchor;
    const parent = anchor.parentElement;
    if (!parent) return;

    const figure = document.createElement('div');
    figure.className = 'md-mermaid';
    figure.appendChild(svg);

    // WHY 先插 figure 再搬走 anchor：`insertBefore` 要求锚点当时还在父节点上，
    // 而把 anchor 移进 details 之后它就不再是父节点的子节点了，顺序反过来会抛
    // NotFoundError——并且是在「图已经渲染成功」之后才抛，看起来像图渲染坏了。
    parent.insertBefore(figure, anchor);

    const details = document.createElement('details');
    details.className = 'md-mermaid-source';
    const summary = document.createElement('summary');
    summary.textContent = '图源码';
    details.appendChild(summary);
    // 直接搬原节点：源码文本一个字都不用重新拼，也就不存在「拼错或再次转义」的可能。
    details.appendChild(anchor);
    figure.appendChild(details);
  }

  /**
   * 降级：代码块原样留在原处，只在它前面加一行原因。
   *
   * @param {object} block `collect` 产出的一项。
   * @param {string} reason 面向用户的原因（不出现堆栈与内部标识）。
   */
  function fallback(block, reason) {
    block.code.setAttribute(STATE_ATTR, 'failed');
    const note = document.createElement('div');
    note.className = 'md-mermaid-error';
    note.textContent = '图未能渲染：' + reason + '（已保留图源码）';
    const parent = block.anchor.parentElement;
    if (parent) parent.insertBefore(note, block.anchor);
    console.warn('[mermaid] 图未能渲染：', reason);
  }

  /**
   * 渲染一个块。
   *
   * @param {object} mermaid 浏览器全局 mermaid。
   * @param {object} block `collect` 产出的一项。
   * @returns {Promise<void>} 始终 resolve：单块失败只影响这一块。
   */
  async function renderBlock(mermaid, block) {
    // 等待库加载期间用户可能切了会话（消息区被清空重建）：这一块已脱离文档，
    // 渲染结果无处安放，继续做只是白烧主线程，还可能把图插进一个已废弃的节点。
    if (!block.code.isConnected) {
      block.code.setAttribute(STATE_ATTR, 'detached');
      return;
    }
    if (!block.ok) {
      fallback(block, block.reason);
      return;
    }

    block.code.setAttribute(STATE_ATTR, 'rendering');
    sequence += 1;
    const renderId = 'mermaid-' + sequence;
    try {
      const result = await mermaid.render(renderId, block.source);
      const svg = extractSvg(result && result.svg);
      if (!svg) throw new Error('渲染结果里没有 svg 节点');
      replaceWithDiagram(block, svg);
      block.code.setAttribute(STATE_ATTR, 'done');
    } catch (err) {
      removeLeftover(renderId);
      fallback(block, describeError(err));
    }
  }

  /**
   * 渲染容器里所有尚未处理的 Mermaid 代码块。
   *
   * 调用方不需要等它：它是正文的锦上添花，且内部已经把每一种失败都变成了可见的降级，
   * 因此**不会**把异常抛给调用方（抛出去只会让调用点的 `catch` 去报一条与用户无关的错）。
   *
   * @param {Element} container 助手气泡或它的正文容器；没有 mermaid 代码块时立即返回。
   * @returns {Promise<void>}
   */
  async function enhance(container) {
    if (!container || typeof container.querySelectorAll !== 'function') return;
    const blocks = collect(container);
    if (!blocks.length) return;

    let mermaid = null;
    try {
      mermaid = await loadLibrary();
    } catch (err) {
      // 资源层面的失败对这一次的所有块都是同一个原因：逐个说明，让每一处都看得出
      // 「源码还在、只是没渲染」，而不是留下一堆看似正常的代码块。
      blocks.forEach((block) => fallback(block, describeError(err)));
      return;
    }

    // WHY 串行：见文件头的第 4 条取舍。
    for (let index = 0; index < blocks.length; index += 1) {
      await renderBlock(mermaid, blocks[index]);
    }
  }

  return {
    enhance: enhance,
    isMermaidCode: isMermaidCode,
    diagramSource: diagramSource,
    LANGUAGE: LANGUAGE,
    STATE_ATTR: STATE_ATTR,
    MAX_SOURCE_CHARS: MAX_SOURCE_CHARS,
    VENDOR_URL: VENDOR_URL,
  };
});
