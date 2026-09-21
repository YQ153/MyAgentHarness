/**
 * 工作区「根作用域」的回归（node --test）。
 *
 * WHY 用真模块做断言：这里要验的恰恰是「换会话时那四份界面缓存有没有清干净」，
 * 而复刻一套逻辑只会让断言通过在一个与产品无关的地方（与 markdown / 凭据两支同一取舍）。
 *
 * WHY 会走到这里：用户报的现象是「切换会话时工作区没有同步切换」——面板左上角的路径
 * 每次重取所以是对的，而目录树是按**虚拟路径**缓存的（`/` 在两条会话下都合法却指向
 * 不同目录），于是面板变成「写着 B、列着 A」。本文件把「换根要丢什么」钉死在测试里。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const require = createRequire(import.meta.url);
const WorkspaceScope = require('../../interfaces/web/static/workspace_scope.js');

/** 造一个「用过的」作用域：四份缓存里都有内容，模拟用户已经点开过目录树与预览。 */
function usedScope(generation) {
  return {
    generation: generation,
    dirs: { '/': { entries: [{ path: '/a.txt' }] }, '/src': { entries: [] } },
    expanded: { '/': true, '/src': true },
    selected: '/a.txt',
    text: { path: '/a.txt', offset: 42 },
  };
}

// ------------------------------------------------------------------ 初始形态

test('没有上一个作用域时从 1 开始', () => {
  const scope = WorkspaceScope.begin(null);

  assert.equal(scope.generation, 1);
});

test('缺省参数（undefined）也当作没有上一个作用域', () => {
  // WHY 单列：`begin(state.workspace)` 在初始化早于状态定义时会是 undefined，
  // 而 `undefined.generation` 会直接抛错、让整页白屏——所以这里必须是容错的。
  const scope = WorkspaceScope.begin(undefined);

  assert.equal(scope.generation, 1);
});

test('新作用域的四份缓存一律为空', () => {
  const scope = WorkspaceScope.begin(null);

  assert.deepEqual(Object.keys(scope.dirs), []);
  assert.deepEqual(Object.keys(scope.expanded), []);
  assert.equal(scope.selected, null);
  assert.equal(scope.text, null);
});

// ------------------------------------------------------------------ 换根

test('代号递增：连续换根不会退回旧代号', () => {
  let scope = WorkspaceScope.begin(null);
  const seen = [scope.generation];
  for (let i = 0; i < 4; i += 1) {
    scope = WorkspaceScope.begin(scope);
    seen.push(scope.generation);
  }

  assert.deepEqual(seen, [1, 2, 3, 4, 5]);
  // WHY 递增而不是「正负翻转」：代号只有单调递增时，「旧的在途响应」才永远不会与
  // 某个未来作用域的代号撞上——撞上就意味着旧目录的内容被写进新会话的面板。
});

test('换根清空已经装过内容的目录树与预览游标', () => {
  const used = usedScope(7);

  const next = WorkspaceScope.begin(used);

  assert.deepEqual(Object.keys(next.dirs), [], '目录缓存必须整份丢掉');
  assert.deepEqual(Object.keys(next.expanded), [], '展开状态属于旧的根');
  assert.equal(next.selected, null);
  assert.equal(next.text, null, '预览续取游标属于旧的那个文件');
});

test('新作用域与旧作用域不共享容器', () => {
  const used = usedScope(3);

  const next = WorkspaceScope.begin(used);

  // WHY 这条最关键：把实现写成「就地清空 previous.dirs」也能让上面两条通过，
  // 但那个实现下旧引用与新的作用域**是同一份容器**——任何一处晚到的写入都会
  // 同时污染两个作用域，而「谁写进去的」再也查不出来。
  assert.notEqual(next.dirs, used.dirs);
  assert.notEqual(next.expanded, used.expanded);

  next.dirs['/'] = { entries: [] };
  assert.deepEqual(Object.keys(used.dirs), ['/', '/src'], '旧作用域不应被新的一侧改动');
});

// ------------------------------------------------------------------ 在途响应

test('同一代号的在途响应算有效', () => {
  const scope = WorkspaceScope.begin(null);

  assert.equal(WorkspaceScope.isCurrent(scope, scope.generation), true);
});

test('换根之后，旧代号的在途响应一律作废', () => {
  const previous = WorkspaceScope.begin(null);
  const inFlight = previous.generation;

  const current = WorkspaceScope.begin(previous);

  assert.equal(WorkspaceScope.isCurrent(current, inFlight), false);
});

test('没有作用域对象时一律判为过期', () => {
  // WHY 缺省判过期而不是判有效：调用方忘记传（或状态尚未就绪）时，「什么都不渲染」
  // 是安全的；判有效会把一份来源不明的数据当成当前根的内容画上去。
  assert.equal(WorkspaceScope.isCurrent(null, 1), false);
  assert.equal(WorkspaceScope.isCurrent(undefined, 1), false);
});

// ------------------------------------------------------------------ 浏览器挂载

test('浏览器环境下挂到全局 WorkspaceScope 上', () => {
  // WHY 单列：上面的用例走的是 CommonJS 分支（``module.exports``），而**浏览器走的是
  // 另一条**（没有 ``module`` 时挂到 ``self``）。``app.js`` 依赖的正是后者——它解析时
  // 就会调用 ``WorkspaceScope.begin``。这条断言把「全局叫什么名字」钉住，而
  // ``test_frontend_wiring`` 只验加载顺序，管不了名字。
  const source = readFileSync(
    new URL('../../interfaces/web/static/workspace_scope.js', import.meta.url),
    'utf8'
  );
  const sandbox = {};
  sandbox.self = sandbox; // 浏览器里 `typeof self !== 'undefined'`

  runInNewContext(source, sandbox);

  assert.equal(typeof sandbox.WorkspaceScope, 'object', '应当挂到全局 WorkspaceScope 上');
  assert.equal(typeof sandbox.WorkspaceScope.begin, 'function');
  assert.equal(sandbox.WorkspaceScope.begin(null).generation, 1);
});
