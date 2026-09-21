/**
 * 会话的「文件根作用域」：一个根对应一份界面缓存。
 *
 * WHY 需要它：工作区面板的界面状态有四份——路径文案、顶栏悬停提示、按虚拟路径缓存的
 * 目录树、以及那个文件预览——它们全都只对**一个根**成立。换会话就是换根，而这四份里
 * 漏清任何一份的表现都是「面板里出现上一条会话的文件」，且不报错：
 *
 * - ``dirs`` 是按**虚拟路径**缓存的（``/``、``/src``），而服务端对每个根都从 ``/`` 开始
 *   编号——于是 ``/`` 在两条会话下都合法却指向不同目录。复用缓存等于把 A 的文件列表
 *   贴进 B 的树里，而左上角的路径文案是对的（它每次都重取），于是现场是「写着 B、
 *   列着 A」，没有任何一处会报错。
 *
 * WHY 单独成模块而不是几行写在 app.js 里：app.js 通篇是 DOM 与 fetch，没有浏览器跑
 * 不起来，于是「换根到底清干净了没有」这件事就只能靠真人点界面来验。把这条规则收成
 * 一个纯函数，就可以被 ``node --test`` 直接驱动（与 markdown.js 是同一个取舍）。
 *
 * WHY 还要一个代号（``generation``）：清缓存只解决「已经拿到的旧数据」，解决不了**在途
 * 的**响应——切到 B 之后 A 的请求才返回，它会把自己的目录写进 B 的树里，而且这次连
 * 「清空」都发生在它之前。代号让迟到者可以自己判断「我已经过期了」并丢弃结果。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.WorkspaceScope = api;
})(typeof self !== 'undefined' ? self : this, function () {
  /**
   * 开始一个新的根作用域：代号 +1，四份缓存清空。
   *
   * Args:
   *   previous: 上一个作用域对象；``null`` / 缺省 / 没有代号时从 1 开始。
   *
   * Returns:
   *   全新的作用域对象 ``{generation, dirs, expanded, selected, text}``。
   *
   * WHY 返回新对象而不是就地清空：就地清空会让「谁持有旧作用域」变得无法判断——
   * 而判断这件事正是下面 ``isCurrent`` 存在的理由。新对象让旧引用天然失效。
   */
  function begin(previous) {
    const previousGeneration =
      previous && typeof previous.generation === 'number' ? previous.generation : 0;
    return {
      generation: previousGeneration + 1,
      dirs: {},
      expanded: {},
      selected: null,
      text: null,
    };
  }

  /**
   * 某个在途请求是否仍属于当前作用域。
   *
   * Args:
   *   scope: 当前作用域对象。
   *   generation: 请求发出时记下的代号。
   *
   * Returns:
   *   ``true`` 表示结果仍然属于当前的根，可以渲染；``false`` 表示期间换过会话，
   *   调用方必须丢弃这份结果。
   */
  function isCurrent(scope, generation) {
    return !!scope && scope.generation === generation;
  }

  return { begin: begin, isCurrent: isCurrent };
});
