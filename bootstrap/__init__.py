"""装配层：全应用唯一的对象组装点。

WHY 独立成包：对象组装需要同时访问 ``runtime``（基础设施）、``agent``（图装配）
与 ``application``（服务）。若把组装放在 ``interfaces`` 层，接口适配器会同时
承担「协议转换」与「依赖组装」两重职责，并导致 ``interfaces`` 直接依赖
``runtime``，破坏分层约定。

把组装收敛到本包后，依赖方向恢复为：

    interfaces → application → runtime
    bootstrap  → 所有层（唯一的例外，因为组装必须看见所有层）
"""

from bootstrap.context import AppContext
from bootstrap.core import build_app_context

__all__ = ["AppContext", "build_app_context"]
