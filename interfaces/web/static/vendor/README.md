# 前端第三方发行文件（vendored）

本目录存放**直接放进页面的第三方发行文件**，与 `static/` 下的自研脚本分开，理由是两条：

1. `tests/frontend/test_static_js_contract.py` 会给 `static/*.js` 施加「调用的名字必须在本文件
   声明过」的契约。那份契约是为**我们写的**代码准备的——拿它去量压缩后的第三方代码，
   只会得到一屏与产品无关的误报，而一条会误报的断言很快就会被当成噪音忽略。
   本目录不在那个 glob 的深度上（`glob("*.js")` 不下钻），因此天然豁免；豁免本身由
   `tests/frontend/test_vendor_bundle.py` 钉住，防止有人把 glob 改成 `rglob` 之后没意识到后果。
2. 第三方文件带自己的版本与许可，和我们的代码分开登记才好升级、好审计。

## mermaid.min.js

| 项目 | 值 |
| --- | --- |
| 上游 | https://github.com/mermaid-js/mermaid |
| 版本 | 11.12.0 |
| 文件 | `dist/mermaid.min.js`（UMD / esbuild IIFE，末尾以 `globalThis["mermaid"] = ...` 暴露全局 `mermaid`） |
| 来源 | https://cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.min.js |
| 许可 | MIT（见上游 `LICENSE`；发行文件内保留了各内联依赖的许可注释） |
| 大小 | 2748992 字节 |
| sha256 | 07e37dfa97b337ccc85365d57eddf99b9706f09db3b59b260d0333b23b343c4b |

选型依据（实测，非推测）：

- **为什么内置而不是走 CDN**：CDN 意味着每次打开会话都依赖第三方的可达性与它的当前版本；
  离线 / 内网部署会直接退化成「图永远渲染不出来」，而供应链上多出一个随时可变的执行源。
  这与 `markdown.js` 开头写明的同一条理由一致。
- **为什么不引打包器**：整个前端没有构建步骤（页面直接挂 `static/*.js`）。内置一份发行文件
  是唯一能同时满足「离线可用」与「不引入构建链」的做法。
- **为什么是 11.12.0 而不是当时最新的 12.0.0**：实测两者体积差一倍（2.75 MB vs 5.58 MB），
  而 v12 是刚发布的大版本；这里要的只是「把模型给出的图渲染出来」，没有必要承担
  首发大版本的回归面（v12 起 `engines.node >= 22.12`，也与本仓 CI 的 node 版本绑得更紧）。
- **为什么按需注入而不是写进 `index.html` 的 `<script>`**：2.7 MB 的解析代价只该由真正
  看到图的会话支付（见 `interfaces/web/static/mermaid_render.js` 的 `loadLibrary`）。

### 升级步骤

1. 重新下载对应版本到本目录，覆盖 `mermaid.min.js`。
2. 更新上表的版本 / 大小 / sha256（Windows：`Get-FileHash .\mermaid.min.js -Algorithm SHA256`）。
3. 确认发行文件末尾仍以 `globalThis["mermaid"] = ...` 导出全局（新版本若改成纯 ESM，
   页面的按需加载方式必须一并改掉）。
4. 跑 `pytest tests/frontend -q`。哈希与上表不一致会直接失败——那正是要让「换了一份文件」
   这件事在评审里可见，而不是静默生效。
