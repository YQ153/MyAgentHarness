/**
 * 极简 Markdown 渲染器（助手消息用）。
 *
 * 安全模型：**先整段转义，再只插入我们自己生成的标签**。
 *  1. 第一步把 `& < > " '` 全部转成实体，于是模型输出里的任何 HTML 都变成惰性文本；
 *  2. 第二步只在**已转义**的文本上做块级 / 行内识别，插入的标签全部来自本文件的白名单；
 *  3. 唯一的注入面是链接地址（模型可控且会进入属性），因此单独做 scheme 白名单，
 *     并把地址里的控制字符与空白剥掉——`java\nscript:` 在浏览器里就是 `javascript:`。
 *
 * WHY 自己写而不是引第三方 Markdown 库：整个前端没有构建步骤。引库要么走 CDN
 * （供应链与离线都成问题），要么为它引入打包器。这里的子集（代码块、列表、表格、
 * 标题、行内代码 / 粗斜体 / 链接）覆盖了模型实际会用的写法。
 *
 * WHY 不做「Markdown 全语法」：每多支持一种语法就多一处正则与逃逸面的交互，
 * 而这里的收益只是排版好看一点——安全边界比排版完整重要。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Markdown = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /** 允许出现在链接里的 scheme。其余（javascript / data / vbscript 等）一律不生成 <a>。 */
  const ALLOWED_SCHEMES = new Set(['http', 'https', 'mailto']);

  const FENCE = /^\s*```(.*)$/;
  const HEADING = /^(#{1,6})\s+(.*)$/;
  const UNORDERED = /^\s*[-*+]\s+(.*)$/;
  const ORDERED = /^\s*(\d+)\.\s+(.*)$/;
  const TABLE_ROW = /^\s*\|(.+)\|\s*$/;
  const TABLE_SEP = /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/;
  const RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
  const LINK = /\[([^\]]*)\]\(([^)\s]+)\)/g;

  /** 把文本转成 HTML 实体。渲染的第一步，也是唯一一处「把内容变成惰性」的地方。 */
  function escapeHtml(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /**
   * 校验并规整链接地址；不允许时返回空串。
   *
   * WHY 先剥控制字符与空白：URL 解析会忽略它们，`java\tscript:alert(1)` 与
   * `javascript:alert(1)` 在浏览器里是同一个东西——不剥掉，白名单就是摆设。
   * 返回的是剥过之后的地址，因为带换行的地址写进属性里同样会被浏览器重新拼回来。
   */
  function safeUrl(raw) {
    const cleaned = String(raw === undefined || raw === null ? '' : raw).replace(
      /[\u0000-\u0020\u007f]/g,
      ''
    );
    if (!cleaned) return '';

    const scheme = /^([a-zA-Z][a-zA-Z0-9+.\-]*):/.exec(cleaned);
    // 没有 scheme 视为相对路径（同源），允许
    if (!scheme) return cleaned;
    return ALLOWED_SCHEMES.has(scheme[1].toLowerCase()) ? cleaned : '';
  }

  /**
   * 行内元素：行内代码 → 链接 → 粗体 → 斜体。
   *
   * WHY 先按行内代码切段：代码里出现的 `*` 与 `[]` 是字面量，若先做粗斜体或链接，
   * 会把 `` `a*b*` `` 渲染成粗体——那是把用户的字面内容改写成了排版。
   */
  function inline(text) {
    return String(text)
      .split(/(`[^`]*`)/g)
      .map((part) => {
        if (part.length > 1 && part.charAt(0) === '`' && part.charAt(part.length - 1) === '`') {
          return '<code>' + part.slice(1, -1) + '</code>';
        }
        return linkify(part);
      })
      .join('');
  }

  /** 链接与粗斜体。链接先处理：它的显示文本里可能带 `*`。 */
  function linkify(segment) {
    let out = segment.replace(LINK, (whole, label, url) => {
      const safe = safeUrl(url);
      // 地址不安全时不生成 <a>：降级成原文，而不是留一个点了会执行脚本的链接
      if (!safe) return whole;
      return (
        '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + label + '</a>'
      );
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    out = out.replace(/(^|[\s(])_([^_\s][^_]*)_/g, '$1<em>$2</em>');
    return out;
  }

  /** 把若干行合成一个段落；段内换行保留成 <br>，聊天气泡里这比空格更贴原样。 */
  function paragraph(lines) {
    return '<p>' + inline(lines.join('\n')).replace(/\n/g, '<br>') + '</p>';
  }

  /** 表格：首行是表头，第二行是分隔行，其余是数据行。 */
  function table(rows) {
    const cells = (row) =>
      row
        .replace(/^\s*\|/, '')
        .replace(/\|\s*$/, '')
        .split('|')
        .map((cell) => cell.trim());

    const head = cells(rows[0]);
    const body = rows.slice(2).map(cells);
    let html = '<table><thead><tr>';
    head.forEach((cell) => {
      html += '<th>' + inline(cell) + '</th>';
    });
    html += '</tr></thead><tbody>';
    body.forEach((row) => {
      html += '<tr>';
      row.forEach((cell) => {
        html += '<td>' + inline(cell) + '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table>';
  }

  /**
   * 渲染 Markdown 子集。
   *
   * @param {string} source 原始文本（未转义）。
   * @returns {string} 可直接赋给 innerHTML 的 HTML；其中只含本文件生成的标签。
   */
  function render(source) {
    // 唯一一次转义：之后所有处理都建立在惰性文本上，插入的标签全部是我们自己写的。
    const text = escapeHtml(source).replace(/\r\n?/g, '\n');
    const lines = text.split('\n');
    const out = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      const fence = FENCE.exec(line);

      if (fence) {
        const lang = fence[1].trim();
        const body = [];
        index += 1;
        while (index < lines.length && !FENCE.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1; // 跳过收尾的围栏；没有收尾时也照常结束
        const attr = lang ? ' class="language-' + lang + '"' : '';
        out.push('<pre class="md-code"><code' + attr + '>' + body.join('\n') + '</code></pre>');
        continue;
      }

      const heading = HEADING.exec(line);
      if (heading) {
        // 聊天窗口里不需要 h1：把层级压到 h3 以下，避免助手消息比页面标题还大
        const level = Math.min(3 + heading[1].length, 6);
        out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
        index += 1;
        continue;
      }

      if (RULE.test(line)) {
        out.push('<hr>');
        index += 1;
        continue;
      }

      if (TABLE_ROW.test(line) && index + 1 < lines.length && TABLE_SEP.test(lines[index + 1])) {
        const rows = [];
        while (index < lines.length && TABLE_ROW.test(lines[index])) {
          rows.push(lines[index]);
          index += 1;
        }
        out.push(table(rows));
        continue;
      }

      const ordered = ORDERED.exec(line);
      const unordered = UNORDERED.exec(line);
      if (ordered || unordered) {
        const orderedList = Boolean(ordered);
        const items = [];
        const pattern = orderedList ? ORDERED : UNORDERED;
        while (index < lines.length) {
          const item = pattern.exec(lines[index]);
          if (!item) break;
          items.push('<li>' + inline(orderedList ? item[2] : item[1]) + '</li>');
          index += 1;
        }
        const tag = orderedList ? 'ol' : 'ul';
        out.push('<' + tag + '>' + items.join('') + '</' + tag + '>');
        continue;
      }

      if (!line.trim()) {
        index += 1;
        continue;
      }

      const buffer = [];
      while (index < lines.length) {
        const current = lines[index];
        if (
          !current.trim() ||
          FENCE.test(current) ||
          HEADING.test(current) ||
          RULE.test(current) ||
          ORDERED.test(current) ||
          UNORDERED.test(current) ||
          (TABLE_ROW.test(current) &&
            index + 1 < lines.length &&
            TABLE_SEP.test(lines[index + 1]))
        ) {
          break;
        }
        buffer.push(current);
        index += 1;
      }
      out.push(paragraph(buffer));
    }

    return out.join('');
  }

  return { render: render, escapeHtml: escapeHtml, safeUrl: safeUrl, inline: inline };
});
