/**
 * 浏览器侧凭据模块的回归（node --test）。
 *
 * WHY 用真模块做断言而不是复刻一套逻辑：这里要验的恰恰是「那一份实现是否会把凭据
 * 放对地方」，复刻出来的第二份实现只会让断言通过在一个与产品无关的地方。
 *
 * 断言分两类：
 *  1. **注入语义**——凭据进哪个头、绝不覆盖调用方显式给出的值、空凭据不注入；
 *  2. **降级不变量**——storage 抛异常（Safari 隐私模式 / 禁止站点数据）时读写都
 *     不得把异常抛给调用方，而 `save` 必须如实报告 `stored: false`，否则调用方会
 *     误以为「刷新后还在」而重载页面，把刚拿到的凭据丢掉。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const AuthCredential = require('../../interfaces/web/static/auth_credential.js');

/**
 * localStorage 替身。
 *
 * WHY 自己写而不是用 jsdom 之类：本模块只用到 getItem / setItem / removeItem 三个
 * 方法，为一个三方法的接口引入 DOM 实现，会让这个测试文件多出一份与环境有关的
 * 失败面。
 */
function fakeStorage(initial) {
  const data = new Map(Object.entries(initial || {}));
  return {
    data,
    setItem(name, value) {
      data.set(name, value);
    },
    getItem(name) {
      return data.has(name) ? data.get(name) : null;
    },
    removeItem(name) {
      data.delete(name);
    },
  };
}

/** 读写都抛异常的 storage，模拟「禁止站点数据」。 */
function brokenStorage() {
  return {
    setItem() {
      throw new Error('SecurityError: 站点数据被禁用');
    },
    getItem() {
      throw new Error('SecurityError: 站点数据被禁用');
    },
    removeItem() {
      throw new Error('SecurityError: 站点数据被禁用');
    },
  };
}

// ------------------------------------------------------------------ 归一化

test('normalize 只接受字符串，其余一律视作没有凭据', () => {
  assert.equal(AuthCredential.normalize('  abc  '), 'abc');
  assert.equal(AuthCredential.normalize(''), '');
  assert.equal(AuthCredential.normalize(null), '');
  assert.equal(AuthCredential.normalize(undefined), '');
  assert.equal(AuthCredential.normalize(12345), '');
  assert.equal(AuthCredential.normalize({ key: 'abc' }), '');
});

// ------------------------------------------------------------------ 存取

test('save 写入归一化后的值并如实报告 stored', () => {
  const storage = fakeStorage();
  const result = AuthCredential.save(storage, '  harness_abc  ');

  assert.deepEqual(result, { value: 'harness_abc', stored: true });
  assert.equal(storage.getItem(AuthCredential.STORAGE_KEY), 'harness_abc');
});

test('save 对空值不写 storage', () => {
  const storage = fakeStorage();
  const result = AuthCredential.save(storage, '   ');

  assert.deepEqual(result, { value: '', stored: false });
  assert.equal(storage.getItem(AuthCredential.STORAGE_KEY), null);
});

test('storage 不可用时 save 仍返回凭据但标记未落盘', () => {
  // WHY 这条是调用方降级的唯一依据：stored=false 时不能重载页面。
  const result = AuthCredential.save(brokenStorage(), 'harness_abc');

  assert.deepEqual(result, { value: 'harness_abc', stored: false });
});

test('read 取回已保存的凭据并去掉空白', () => {
  const storage = fakeStorage({ [AuthCredential.STORAGE_KEY]: '  harness_abc  ' });

  assert.equal(AuthCredential.read(storage), 'harness_abc');
});

test('read 在 storage 不可用或缺失时返回空串而不抛异常', () => {
  assert.equal(AuthCredential.read(brokenStorage()), '');
  assert.equal(AuthCredential.read(undefined), '');
  assert.equal(AuthCredential.read(fakeStorage()), '');
});

test('clear 删除凭据，storage 不可用时返回 false', () => {
  const storage = fakeStorage({ [AuthCredential.STORAGE_KEY]: 'harness_abc' });

  assert.equal(AuthCredential.clear(storage), true);
  assert.equal(storage.getItem(AuthCredential.STORAGE_KEY), null);
  assert.equal(AuthCredential.clear(brokenStorage()), false);
});

test('存储键带命名空间前缀，避免与同源其它页面撞键', () => {
  assert.equal(AuthCredential.STORAGE_KEY, 'harness.api_key');
});

// ------------------------------------------------------------------ 请求头注入

test('有凭据时注入默认请求头', () => {
  const headers = AuthCredential.headersWithCredential(undefined, 'harness_abc', '');

  assert.deepEqual(headers, { 'X-API-Key': 'harness_abc' });
});

test('请求头名跟随后端配置', () => {
  const headers = AuthCredential.headersWithCredential({}, 'harness_abc', 'X-Custom-Key');

  assert.deepEqual(headers, { 'X-Custom-Key': 'harness_abc' });
});

test('没有凭据时不注入任何请求头', () => {
  assert.deepEqual(AuthCredential.headersWithCredential({ Accept: 'text/plain' }, '', ''), {
    Accept: 'text/plain',
  });
  // 空凭据也不得留下一个值为空的头：那会让服务端把请求当成「带了凭据但无效」
  assert.deepEqual(AuthCredential.headersWithCredential(undefined, '   ', ''), {});
});

test('不覆盖调用方已显式给出的同名头（大小写不敏感）', () => {
  // WHY 显式值优先：探测请求想用 A、实际发出去 B，是那种只看日志永远查不出来的错。
  const headers = AuthCredential.headersWithCredential(
    { 'x-api-key': 'explicit' },
    'harness_abc',
    'X-API-Key'
  );

  assert.deepEqual(headers, { 'x-api-key': 'explicit' });
});

test('保留调用方的其它请求头', () => {
  const headers = AuthCredential.headersWithCredential(
    { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    'harness_abc',
    'X-API-Key'
  );

  assert.deepEqual(headers, {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    'X-API-Key': 'harness_abc',
  });
});

test('Headers 实例与二维数组都被收敛成普通对象', () => {
  const fromInstance = AuthCredential.headersWithCredential(
    new Headers({ 'Content-Type': 'application/json' }),
    'harness_abc',
    'X-API-Key'
  );
  const fromPairs = AuthCredential.headersWithCredential(
    [['Content-Type', 'application/json']],
    'harness_abc',
    'X-API-Key'
  );

  assert.deepEqual(fromInstance, {
    'content-type': 'application/json',
    'X-API-Key': 'harness_abc',
  });
  assert.deepEqual(fromPairs, {
    'Content-Type': 'application/json',
    'X-API-Key': 'harness_abc',
  });
});

test('不修改调用方传入的 headers 对象', () => {
  const original = { Accept: 'text/plain' };

  AuthCredential.headersWithCredential(original, 'harness_abc', 'X-API-Key');

  assert.deepEqual(original, { Accept: 'text/plain' });
});
