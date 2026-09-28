/**
 * Mermaid 渲染模块的纯逻辑回归（node --test）。
 *
 * 范围说明：这里只覆盖**不依赖 DOM 的那部分**——「哪一块算 Mermaid 代码块」与
 * 「取出来的源码值不值得送进渲染器」。真正的绘制要 DOM（mermaid 自己也是靠 DOM 度量的），
 * 在 node 里用桩件假装一个 DOM 去验「图长什么样」，验的是桩件而不是产品。
 * 因此接线（两条渲染路径都调用它、容器怎么被扫到）由 Python 侧用例负责，
 * 这里只钉住那两条**最容易静默出错**的判断。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const MermaidView = require('../../interfaces/web/static/mermaid_render.js');

/** 造一个「像 <code> 那样」的最小对象：只实现本模块真正读的两个成员。 */
function codeWith(className, textContent = '') {
  return {
    getAttribute: (name) => (name === 'class' ? className : null),
    textContent,
  };
}

// ------------------------------------------------------------------ 代码块识别

test('识别 language-mermaid，且大小写不敏感、允许夹在其它 class 之间', () => {
  assert.ok(MermaidView.isMermaidCode(codeWith('language-mermaid')));
  assert.ok(MermaidView.isMermaidCode(codeWith('language-Mermaid')));
  assert.ok(
    MermaidView.isMermaidCode(codeWith('language-mermaid hljs')),
    'class 里有别的 token 时仍应识别'
  );
});

test('不把其它语言或名字相近的 class 当成 Mermaid', () => {
  const cases = ['language-js', 'language-mermaidish', 'mermaid', 'language-', '', null];
  for (const className of cases) {
    assert.ok(
      !MermaidView.isMermaidCode(codeWith(className)),
      `不应识别：${JSON.stringify(className)}`
    );
  }
});

test('传入 null 或缺 getAttribute 的对象时不抛错，只返回 false', () => {
  // WHY 需要这条：这个判断跑在「扫一遍消息区」的循环里，抛一次就会让整条消息的
  // 其余代码块一起失去渲染机会——而那看起来像「只有第一张图能画」。
  assert.equal(MermaidView.isMermaidCode(null), false);
  assert.equal(MermaidView.isMermaidCode({}), false);
});

// ------------------------------------------------------------------ 源码提取

test('源码首尾空白被去掉，中间内容一字不动', () => {
  const parsed = MermaidView.diagramSource(
    codeWith('language-mermaid', '\n  graph TD\n    A --> B\n\n')
  );

  assert.equal(parsed.ok, true);
  assert.equal(parsed.source, 'graph TD\n    A --> B');
  assert.equal(parsed.reason, '');
});

test('空白源码被拒绝，并给出可读原因', () => {
  for (const text of ['', '   ', '\n\t\n']) {
    const parsed = MermaidView.diagramSource(codeWith('language-mermaid', text));

    assert.equal(parsed.ok, false, `不应接受空白源码：${JSON.stringify(text)}`);
    assert.ok(parsed.reason.includes('空'), '原因里要说明是空的');
  }
});

test('空节点（undefined）同样被拒绝而不是抛错', () => {
  const parsed = MermaidView.diagramSource(undefined);

  assert.equal(parsed.ok, false);
  assert.equal(parsed.source, '');
});

test('超长源码被拒绝，但源码本身仍完整返回（降级时要显示它）', () => {
  const huge = 'A-->B;'.repeat(MermaidView.MAX_SOURCE_CHARS);
  const parsed = MermaidView.diagramSource(codeWith('language-mermaid', huge));

  assert.equal(parsed.ok, false, '超长源码不应进入渲染器');
  assert.equal(parsed.source, huge, '拒绝渲染不等于丢掉源码');
  assert.ok(
    parsed.reason.includes(String(MermaidView.MAX_SOURCE_CHARS)),
    '原因里要写出上限，否则用户不知道要删到什么程度'
  );
});

// ------------------------------------------------------------------ 对外契约

test('导出的 enhance 是函数，且状态标记是常量字符串', () => {
  // WHY 需要这条：app.js 只认 MermaidView.enhance 这一个名字，且它自己不带任何
  // 「渲染器在不在」的判断——改名或改成非函数，表现是所有助手消息里的图静默消失。
  assert.equal(typeof MermaidView.enhance, 'function');
  assert.equal(typeof MermaidView.STATE_ATTR, 'string');
  assert.equal(MermaidView.LANGUAGE, 'mermaid');
  // 发行文件必须落在同源静态路径下：换成 CDN 会同时破坏离线可用与供应链边界
  assert.ok(
    MermaidView.VENDOR_URL.startsWith('/vendor/'),
    `内置依赖必须走同源静态路径，当前是 ${MermaidView.VENDOR_URL}`
  );
});
