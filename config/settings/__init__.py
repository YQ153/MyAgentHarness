"""AppConfig 的域 mixin：每个文件承载一个功能域的字段与本域 validator。

WHY 用 mixin 而不是把字段继续留在 ``AppConfig`` 里：``AppConfig`` 是一个
``pydantic-settings`` 的 ``BaseSettings``，字段无法拆成多个独立实例再合并
（否则 ``load`` / ``.env`` 加载 / 未知键告警都要跟着改）。mixin 组合是
pydantic v2 官方支持的形态——每个域一个 ``BaseModel`` 子类只声明字段与
**本域** validator，``AppConfig`` 多继承它们与 ``BaseSettings``，字段与
validator 由框架自动合并。

两条纪律（违反任何一条都会在导入期或行为期炸出难以排查的问题）：

1. **域 mixin 的 ``field_validator`` 只准引用本 mixin 自己的字段**：pydantic
   在 mixin 类创建时就要解析字段名，跨域引用会直接 ``NameError``。跨域校验
   一律上收到 ``AppConfig``（目前没有——原文件唯一的跨域 validator
   ``_parse_csv_lists`` 已按域一分为二：附件 MIME 留在 ``workspace``，
   vision 别名迁到 ``llm``，原注释「分隔符相同但语义不同，故分开注册」的
   意图因此贯彻得更彻底）。
2. **域 mixin 之间互不依赖**：需要常量或工具函数时，从 ``config.constants``
   / ``config.parsing`` 导入，不允许从兄弟域导入——否则域的边界会逐步糊掉。
"""
