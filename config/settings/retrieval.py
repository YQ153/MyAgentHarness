"""检索与语义域配置：内置联网检索/抓取、嵌入后端与知识库索引。

字段从原 ``config.AppConfig`` 的「联网工具（内置检索与抓取）」「嵌入后端
（知识库的语义能力）」「知识库（工作区文档索引与检索）」三个分区
（原 L871–1009）整体迁入。三者同属「把外部内容注入对话上下文」这条链路
（检索结果、嵌入向量与分块参数互相咬合），因此归为一个域。
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from config.enums import EmbeddingBackendKind


class RetrievalSettings(BaseModel):
    """检索与语义域的字段：联网检索/抓取、嵌入后端与知识库分块。"""

    # ---------------- 联网工具（内置检索与抓取） ----------------
    web_search_provider: Literal["none", "tavily", "searxng"] = "none"
    """检索 provider。

    WHY 默认 ``none`` 而不是某个真实 provider：联网检索会把用户的查询词
    发给第三方，默认打开等于替用户做了这个决定。

    ``tavily``：托管检索服务，需要密钥；``searxng``：自建元搜索，需要地址、
    不需要密钥（与 ``ollama`` 同属「显式提供地址即可用」）。
    """

    web_search_api_key: str = Field(default="", repr=False)
    """检索服务密钥；``searxng`` 不需要。"""

    web_search_base_url: str = ""
    """检索服务地址；留空时用 provider 的官方地址（``searxng`` 必须显式提供）。"""

    web_search_timeout_seconds: float = Field(default=15.0, gt=0)
    """单次检索请求的超时秒数。"""

    web_search_max_retries: int = Field(default=2, ge=0, le=5)
    """检索遇到传输层故障时的额外尝试次数（``0`` 表示不重试）。

    WHY 传输层故障必须重试：实测一轮对话里连续 4 次 Tavily 调用，前 3 次 200 OK、
    第 4 次 ``ConnectTimeout``——请求根本没到达服务端，却让模型收到一次失败结果。
    默认值与 ``llm_max_retries`` 同口径，避免出现「模型调用能自愈、联网工具不能」的
    行为差异。

    WHY HTTP 状态码不在此列：``POST /search`` 按次计费，重试一个已被服务端接受的请求
    等于替用户多付一次钱；4xx 重试则注定得到同一个答案。
    """

    web_search_max_results: int = Field(default=5, ge=1, le=20)
    """检索返回的结果条数上限。

    WHY 必须有上限：检索结果会整体进入上下文，条数不设限时一次检索就可能
    挤掉对话历史；上限也直接决定上游计费量。
    """

    web_fetch_timeout_seconds: float = Field(default=20.0, gt=0)
    """单次网页抓取请求的超时秒数。"""

    web_fetch_max_retries: int = Field(default=1, ge=0, le=5)
    """抓取遇到传输层故障时的重试次数。

    WHY 默认 1 而不是与检索同值：抓取的目标是模型给出来的地址，其中混有相当比例的
    坏链接与已下线站点；失败后最有效的动作通常是**换一个来源**，而不是在同一个地址上
    反复重试。抓请求是幂等 GET，重试一次的代价很低，故不为 0。
    """

    web_fetch_max_chars: int = Field(default=20_000, ge=500, le=500_000)
    """抓取正文的字符上限（超出部分截断并在输出中显式标注）。"""

    web_fetch_max_redirects: int = Field(default=3, ge=0, le=10)
    """允许跟随的 HTTP 重定向上限。

    WHY 必须限制：每一跳都是一次新的出站请求，而下一跳的地址由**上一次响应
    的 Location 头**决定——不设上限就等于把「还能访问哪些地址」的控制权交给
    远端，而逐跳校验正是 SSRF 防线中最容易被绕过的一环。
    """

    web_user_agent: str = ""
    """出站请求的 User-Agent；留空时用内置默认值。"""

    # ---------------- 嵌入后端（知识库的语义能力） ----------------
    embedding_backend: EmbeddingBackendKind = EmbeddingBackendKind.NONE
    """嵌入后端档位；各档位取舍见 ``EmbeddingBackendKind``。

    WHY 默认 ``none``：与 ``web_search_provider`` 同一口径——语义嵌入要么把文档内容
    发给第三方服务，要么在本机常驻一个实测 189 MB 的模型进程，两者都是「替用户做的
    决定」，不应跟着默认值开启。
    """

    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    """嵌入模型名。

    ``openai-compat`` 档位下它作为请求体的 ``model`` 字段原样发出（服务端据此定位
    要用的模型）；``subprocess`` 档位下它是 fastembed 的模型标识。
    """

    embedding_base_url: str = ""
    """``openai-compat`` 档位的服务地址（不含 ``/v1``，由实现拼接）。"""

    embedding_api_key: str = Field(default="", repr=False)
    """嵌入服务密钥；本地服务（TEI / Ollama）通常不需要。"""

    embedding_dims: int = Field(default=512, ge=1, le=8192)
    """向量维度。

    WHY 必须是**配置**而不是从首次响应里读回来：维度在建表时就要固定（向量表的列宽），
    而读回来的时机在插入之后——那时表已经建错了。它同时是「换了模型必须重建索引」的
    显式表达：换模型却不改这一项，插入会因维度不符而报错，而不是静默写进一批语义上
    无法互相比较的向量。
    """

    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    """单次嵌入请求的文本条数上限。"""

    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    """单次嵌入往返（HTTP 请求或子进程一次问答）的超时秒数。

    WHY 比 ``llm_timeout`` 短：嵌入处在索引与检索的**同步阻塞**路径上，超时过长会让
    一次检索把整轮对话拖住。
    """

    embedding_idle_seconds: int = Field(default=600, ge=0)
    """``subprocess`` 档位下子进程的空闲回收秒数；``0`` 表示不回收。

    WHY 需要回收：知识库检索是偶发动作（一轮对话可能只在开头检索一次），而模型常驻
    内存实测 189 MB；不回收等于让一次偶发操作永久占住这份内存。
    """

    embedding_python: str = ""
    """``subprocess`` 档位使用的解释器路径。

    留空时用 ``.data/embed-venv`` 下的约定路径（由 ``scripts/setup_embed_venv.py``
    准备）。显式提供是为了让人能把模型装在自己选好的环境里，而不必迁就本项目的约定。
    """

    # ---------------- 知识库（工作区文档索引与检索） ----------------
    knowledge_chunk_chars: int = Field(default=800, ge=100, le=8000)
    """单个分块的目标字符数。

    WHY 以**字符**而不是 token 计量：切分发生在嵌入之前、模型之外，这一层拿不到
    分词器；而中英文的 token/字符比差异很大，用字符才能给出跨语言一致的行为。
    """

    knowledge_chunk_overlap_chars: int = Field(default=120, ge=0, le=2000)
    """相邻分块的重叠字符数。

    WHY 需要重叠：一句话被切断会同时毁掉两边的语义——左块丢了句尾、右块丢了句首，
    于是这句话在两个块里都检索不到。重叠让边界句在某一侧保持完整。
    """

    knowledge_search_top_k: int = Field(default=6, ge=1, le=50)
    """检索返回的块数上限。

    WHY 必须有上限：检索结果整体进入上下文，条数不设限时一次检索就能挤掉对话历史，
    也直接决定上游计费量。
    """

    knowledge_search_max_chars: int = Field(default=4000, ge=200, le=50000)
    """单次检索结果注入对话的**总字符预算**。

    WHY 在条数上限之外还要一个字符预算：``top_k`` 限制的是「几条」，而一条片段
    本身可能上千字，几条加起来仍能顶掉一整轮对话的篇幅——按字符设上限才是真正
    约束「这次检索往上下文里塞了多少」的量纲。超出时宁可少给几条、让模型用更
    具体的检索词再查。

    WHY 这是「注入侧」预算而不是清历史的依据：它只影响本次往对话尾部追加多少，
    不改动已有消息，因此对 DeepSeek 的前缀缓存是中性的——这也是它与一切
    「清历史」类治理的本质区别。
    """

    knowledge_max_chunks_per_document: int = Field(default=500, ge=1, le=10000)
    """单个文档允许的分块数上限。

    WHY 必须有：分块数直接决定嵌入调用次数与向量表体积，而工作区里出现一份几 MB 的
    日志或生成文件是常事。超限时截断并记日志，而不是让一次索引把配额打满。
    """

    @field_validator("embedding_backend", mode="before")
    @classmethod
    def _normalize_embedding_backend(cls, value: object) -> object:
        """容错大小写与空白，与 ``execution_mode`` 保持同一口径。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _validate_embedding_backend(self) -> RetrievalSettings:
        """按档位校验必需字段。

        WHY 在加载期拦而不是等首次嵌入：``openai-compat`` 缺地址时，异常会发生在
        一次索引或检索的深处，栈顶指向网络层；而配置自身的问题应当在启动时就能被
        指出来（与 ``MCPServerSpec`` 按传输方式校验同一个理由）。

        Raises:
            ValueError: ``openai-compat`` 档位未提供 ``embedding_base_url``。
        """
        if self.embedding_backend is EmbeddingBackendKind.OPENAI_COMPAT and not self.embedding_base_url.strip():
            raise ValueError(
                "EMBEDDING_BACKEND=openai-compat 必须提供 EMBEDDING_BASE_URL"
                "（例：容器内的嵌入服务 http://embed:80）"
            )
        return self

    @model_validator(mode="after")
    def _validate_knowledge_chunking(self) -> RetrievalSettings:
        """校验分块参数的自洽性。

        WHY 在加载期拦下：重叠大于等于块长时，切分会在同一处反复推进而无法前进
        ——表现为索引卡死或产出满天飞的重复块，而原因要到切分器内部才看得出来。

        Raises:
            ValueError: 重叠字符数不小于块长。
        """
        if self.knowledge_chunk_overlap_chars >= self.knowledge_chunk_chars:
            raise ValueError(
                f"KNOWLEDGE_CHUNK_OVERLAP_CHARS（{self.knowledge_chunk_overlap_chars}）"
                f"必须小于 KNOWLEDGE_CHUNK_CHARS（{self.knowledge_chunk_chars}），"
                "否则切分会原地打转"
            )
        return self
