---
name: commit-message
description: 为当前改动生成规范的 Git 提交信息（Conventional Commits）。当用户要求写提交信息、准备提交代码、整理本次改动，或问「这次改了什么」时使用。
---

# 提交信息规范

## 何时使用

- 用户明确要求写提交信息、或准备提交/整理改动；
- 一组修改完成后需要说明「这次改了什么」。

## 格式

```text
<type>(<scope>): <subject>

<body>

<footer>
```

## 规则

1. **type 只能取**：`feat` / `fix` / `refactor` / `perf` / `test` / `docs` / `build` / `chore`
2. **scope**：受影响的模块名（如 `skills`、`run`、`web`）；跨模块时省略括号
3. **subject**：祈使句、不加句号、不超过 50 字符；说清**做了什么**，而不是罗列改了哪些文件
4. **body**：说明**为什么**这样改；一句话一件事，用 `-` 列点
5. **footer**：破坏性变更写 `BREAKING CHANGE:`，关联 issue 写 `Refs: #123`

## 步骤

1. 先用 `git status` 与 `git diff` 看**全部**改动（不要只看自己最后编辑的文件）；
2. 归纳改动的**意图**，据此挑 type；若包含互不相关的意图，建议拆成多次提交而不是硬凑一条；
3. 先写 subject，再补 body 里的「为什么」；
4. 收尾核对：不读 diff 的情况下，subject 能否说清这次改动的目的。

## 反例

- `fix: 修复 bug` —— 没说清是什么 bug、在什么场景下发生；
- `update files` —— 无 type、无意图；
- subject 里塞三个不相关的改动 —— 应当拆分提交。
