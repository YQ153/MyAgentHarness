/**
 * 浏览器侧的 API Key 凭据：存取与请求头注入。
 *
 * WHY 需要它：apikey 模式下的凭据「由调用方持有」，而浏览器也是一种调用方——它必须
 * 自己把 key 放进请求头。服务端没有登录页可跳（凭据不在服务端，"登出" 也只是记一笔
 * 审计），所以凭据只能由客户端保存并随每个请求带上。
 *
 * 安全模型：
 *  1. 凭据只写进**本机浏览器的 localStorage**，键名固定、只被本页面读取。前端没有
 *     外链脚本与 CDN，markdown 渲染器自带 scheme 白名单，因此不存在「把凭据送给
 *     第三方」的通路；但反过来说，任何能在这个源上执行脚本的漏洞都能读到它——这
 *     正是它不落进服务端会话、也不写进 URL 的原因；
 *  2. 注入时**不覆盖**调用方已显式给出的同名头（大小写不敏感）：显式值优先，避免
 *     「探测请求想带 A，发出去却变成 B」这类静默改写；
 *  3. 读写 localStorage 一律吞异常：Safari 隐私模式与「禁止站点数据」会让它抛
 *     SecurityError。此时降级为「本次页面内存有效」（调用方把值放进自己的状态里），
 *     而不是让整个界面崩掉。
 *
 * WHY 与 app.js 分开成文件：本模块全是纯函数（storage 由调用方传入），可以被
 * ``node --test`` 直接驱动；app.js 通篇是 DOM 与 fetch，没有浏览器就跑不起来。
 * 这与 markdown.js 是同一个取舍。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AuthCredential = api;
})(typeof self !== 'undefined' ? self : this, function () {
  /** localStorage 里的键名；带命名空间前缀，避免与同源其它页面的键相撞。 */
  const STORAGE_KEY = 'harness.api_key';

  /** 后端 ``auth_api_key_header`` 的默认值；实际头名由 ``/auth/config`` 给出。 */
  const DEFAULT_HEADER = 'X-API-Key';

  /** 归一化：只接受字符串，两侧空白去掉；其余一律视作「没有凭据」。 */
  function normalize(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  /** 读取已保存的凭据；读不到或 storage 不可用时返回空串。 */
  function read(storage) {
    try {
      return normalize(storage && storage.getItem(STORAGE_KEY));
    } catch (err) {
      return '';
    }
  }

  /**
   * 保存凭据。
   *
   * Returns:
   *   ``{ value, stored }``——``value`` 是归一化后的凭据（可能为空串），
   *   ``stored`` 表示是否真的落到了 storage。二者分开是因为「写不进去」与
   *   「没写」对调用方含义不同：前者仍应让本次页面可用，只是刷新后要重贴。
   */
  function save(storage, value) {
    const key = normalize(value);
    if (!key) {
      return { value: '', stored: false };
    }
    try {
      storage.setItem(STORAGE_KEY, key);
      return { value: key, stored: true };
    } catch (err) {
      return { value: key, stored: false };
    }
  }

  /** 清除已保存的凭据；受 storage 不可用影响时返回 false。 */
  function clear(storage) {
    try {
      storage.removeItem(STORAGE_KEY);
      return true;
    } catch (err) {
      return false;
    }
  }

  /**
   * 把任意形态的 headers 收敛成普通对象。
   *
   * WHY 三种形态都要认：``fetch`` 的 ``headers`` 允许普通对象、``Headers`` 实例与
   * 二维数组。只处理其中一种的话，另一种会被静默丢掉——而丢掉凭据的表现是「一直
   * 未认证」，与「key 不对」看起来一模一样，排查要多绕一圈。
   */
  function toPlainHeaders(headers) {
    const result = {};
    if (!headers) {
      return result;
    }
    if (typeof Headers !== 'undefined' && headers instanceof Headers) {
      headers.forEach((value, name) => {
        result[name] = value;
      });
      return result;
    }
    if (Array.isArray(headers)) {
      headers.forEach((pair) => {
        if (Array.isArray(pair) && pair.length === 2) {
          result[pair[0]] = pair[1];
        }
      });
      return result;
    }
    Object.keys(headers).forEach((name) => {
      result[name] = headers[name];
    });
    return result;
  }

  /**
   * 生成带凭据的请求头。
   *
   * Args:
   *   headers: 调用方原有的请求头；可为 ``undefined`` / 普通对象 / ``Headers`` / 二维数组。
   *   key: 当前凭据；空串表示未认证，此时不注入任何头。
   *   headerName: 请求头名；空值回落到 ``X-API-Key``。
   *
   * Returns:
   *   新的普通对象；**不修改入参**，且调用方已给出的同名头保持原值。
   */
  function headersWithCredential(headers, key, headerName) {
    const result = toPlainHeaders(headers);
    const credential = normalize(key);
    const name = normalize(headerName) || DEFAULT_HEADER;
    if (!credential) {
      return result;
    }
    const exists = Object.keys(result).some(
      (existing) => existing.toLowerCase() === name.toLowerCase()
    );
    if (!exists) {
      result[name] = credential;
    }
    return result;
  }

  return {
    STORAGE_KEY: STORAGE_KEY,
    DEFAULT_HEADER: DEFAULT_HEADER,
    normalize: normalize,
    read: read,
    save: save,
    clear: clear,
    headersWithCredential: headersWithCredential,
  };
});
