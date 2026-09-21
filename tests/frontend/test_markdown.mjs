/**
 * Markdown 渲染器的安全回归（node --test）。
 *
 * WHY 用真渲染器的输出做断言而不是复刻一套逻辑：这里要验的恰恰是「那一份实现是否
 * 安全」，复刻出来的第二份实现只会让断言通过在一个与产品无关的地方。
 *
 * 断言分两类：**具体攻击面**（脚本标签、危险 scheme）与**通用不变量**
 * （输出里除白名单标签外不得残留任何 `<`）——后者能挡住我没想到的那些构造。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Markdown = require('../../interfaces/web/static/markdown.js');

/** 渲染器允许生成的标签，其余一律视为逃逸。 */
const ALLOWED_TAGS =
  /<\/?(?:p|br|hr|h[3-6]|ul|ol|li|strong|em|code|pre|table|thead|tbody|tr|th|td|a)(?:\s[^>]*)?>/g;

/** 通用不变量：剥掉白名单标签后，输出里不应再有 `<`。 */
function assertOnlyWhitelistedTags(html) {
  const leftover = html.replace(ALLOWED_TAGS, '');
  assert.ok(
    !leftover.includes('<'),
    `输出里出现了白名单之外的标签：${leftover}`
  );
}

// ------------------------------------------------------------------ 转义

test('脚本标签变成惰性文本，不产生 DOM 节点', () => {
  const html = Markdown.render('<script>alert(1)</script>');

  assert.ok(!html.includes('<script'), '输出里不能有可执行的 script 标签');
  assert.ok(html.includes('&lt;script&gt;'), '应当以实体形式保留原文');
  assertOnlyWhitelistedTags(html);
});

test('图片标签里的 onerror 不会成为属性', () => {
  const html = Markdown.render('<img src=x onerror=alert(1)>');

  // WHY 只断言「没有裸标签」：转义后的输出里，`onerror=` 这几个字符**本来就会**作为
  // 普通文本出现（`&lt;img src=x onerror=…&gt;`），那是惰性文本而不是属性。
  // 断言它不出现等于在断言「转义没发生」——恰好把安全行为判成失败。
  assert.ok(!html.includes('<img'), '不应出现可执行的 img 标签');
  assert.ok(html.includes('&lt;img'), '应当以实体形式保留原文');
  assertOnlyWhitelistedTags(html);
});

test('正文里的尖括号与引号都被转义', () => {
  const html = Markdown.render('a < b && c > d "x" \'y\'');

  assert.ok(html.includes('&lt;'));
  assert.ok(html.includes('&amp;&amp;'));
  assert.ok(html.includes('&quot;'));
  assertOnlyWhitelistedTags(html);
});

// ------------------------------------------------------------------ 链接 scheme

test('javascript: 链接被拒绝，降级成原文', () => {
  const html = Markdown.render('[点我](javascript:alert(1))');

  assert.ok(!/<a[^>]*href/i.test(html), '不应生成任何链接');
  assert.ok(html.includes('点我'));
  assertOnlyWhitelistedTags(html);
});

test('用制表符/换行拆开的 javascript: 同样被拒绝', () => {
  // URL 解析会忽略这些空白，`java\tscript:` 与 `javascript:` 在浏览器里是同一个东西
  for (const raw of ['[x](java\tscript:alert(1))', '[x](java\nscript:alert(1))']) {
    const html = Markdown.render(raw);

    assert.ok(!/href="[^"]*javascript/i.test(html), `未被拦截：${raw}`);
    assert.ok(!/<a[^>]*href/i.test(html), `不应生成链接：${raw}`);
  }
});

test('data: 与 vbscript: 链接被拒绝', () => {
  for (const raw of ['[x](data:text/html;base64,PHNjcmlwdD4=)', '[x](vbscript:msgbox(1))']) {
    const html = Markdown.render(raw);

    assert.ok(!/<a[^>]*href/i.test(html), `不应生成链接：${raw}`);
  }
});

test('http(s) 与相对路径链接保留，且属性中的 & 已转义', () => {
  const absolute = Markdown.render('[官网](https://example.com?a=1&b=2)');
  const relative = Markdown.render('[文档](/docs/usage.md)');
  const mail = Markdown.render('[来信](mailto:a@b.com)');

  assert.ok(absolute.includes('href="https://example.com?a=1&amp;b=2"'));
  assert.ok(absolute.includes('rel="noopener noreferrer"'));
  assert.ok(relative.includes('href="/docs/usage.md"'));
  assert.ok(mail.includes('href="mailto:a@b.com"'));
  assertOnlyWhitelistedTags(absolute);
});

// ------------------------------------------------------------------ 块级语法

test('围栏代码块里的内容不被当作行内语法处理', () => {
  const html = Markdown.render('```js\nconst a = **not bold**;\n<img src=x onerror=1>\n```');

  assert.ok(html.includes('<pre class="md-code"><code class="language-js">'));
  assert.ok(html.includes('**not bold**'), '代码块内不得再做粗体解析');
  assert.ok(html.includes('&lt;img'), '代码块内的标签同样要转义');
  assert.ok(!html.includes('<img'));
  assertOnlyWhitelistedTags(html);
});

test('未闭合的围栏也能正常收尾', () => {
  const html = Markdown.render('```\nabc');

  assert.ok(html.includes('abc'));
  assertOnlyWhitelistedTags(html);
});

test('列表渲染成 ul / ol', () => {
  const unordered = Markdown.render('- 甲\n- 乙');
  const ordered = Markdown.render('1. 甲\n2. 乙');

  assert.ok(unordered.includes('<ul><li>甲</li><li>乙</li></ul>'));
  assert.ok(ordered.includes('<ol><li>甲</li><li>乙</li></ol>'));
});

test('表格渲染成 table，且表头与数据行分开', () => {
  const html = Markdown.render('| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |');

  assert.ok(html.includes('<table><thead><tr><th>名称</th><th>值</th></tr></thead>'));
  assert.ok(html.includes('<tbody>'));
  assert.ok(html.includes('<td>a</td>'));
  assertOnlyWhitelistedTags(html);
});

test('行内代码保护其中的星号', () => {
  const html = Markdown.render('`a*b*` 与 **粗体**');

  assert.ok(html.includes('<code>a*b*</code>'));
  assert.ok(html.includes('<strong>粗体</strong>'));
});

test('标题层级被压到 h3 以下', () => {
  const html = Markdown.render('# 一级标题');

  assert.ok(html.includes('<h4>一级标题</h4>'), '聊天里不该出现 h1/h2');
});
