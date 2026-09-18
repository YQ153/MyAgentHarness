"""在容器内的 Authentik 里声明本项目的 OIDC 应用与提供方（可重复执行）。

用法（仓库根目录）：

    docker compose --profile oidc exec -T server ak shell < scripts/bootstrap_authentik.py

WHY 做成脚本而不是「README 里点几下界面」：IdP 侧的这几项配置是应用能否登录的**前提**，
而手工配置无法复现、也无法复核。做成脚本后它进入版本控制，重建环境只需一条命令。

WHY 用 ``.env`` 里的 client_id / client_secret 而不是在这里随机生成：随机生成意味着
「跑完还要把值抄回 .env」，那一步必然被漏。这里反过来——由 ``.env`` 决定，脚本负责让
IdP 与之对齐。任何足够随机的串都可以，不必是 Authentik 自己生成的那个。

WHY 必须显式设置 ``grant_types``：这是真机验证抓到的一处**只有跑一次才会发现**的问题——
用 ORM 直接创建的提供方，``grant_types`` 是**空列表**（界面上它是勾选项，创建时会默认勾上），
于是授权码流程会被 IdP 拒绝：日志里是 ``Invalid grant_type for provider``，
而调用方只看到一个 ``invalid_request``。排查它花的时间足够说明这条注释值得写在这里。
"""

import os

from authentik.core.models import Application
from authentik.flows.models import Flow
from authentik.providers.oauth2.models import (
    OAuth2Provider,
    RedirectURI,
    RedirectURIMatchingMode,
    ScopeMapping,
)

NAME = "myagentharness"
AUTHORIZATION_FLOW = "default-provider-authorization-implicit-consent"
INVALIDATION_FLOW = "default-provider-invalidation-flow"

client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
client_secret = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
redirect_uri = os.environ.get(
    "OIDC_REDIRECT_URI", "http://localhost:8000/auth/callback"
).strip()

if not client_id or not client_secret:
    raise SystemExit(
        "OIDC_CLIENT_ID / OIDC_CLIENT_SECRET 未设置。请先在 .env 里填好这两个值"
        "（任意足够随机的串即可），再跑本脚本。"
    )

try:
    authorization_flow = Flow.objects.get(slug=AUTHORIZATION_FLOW)
    invalidation_flow = Flow.objects.get(slug=INVALIDATION_FLOW)
except Flow.DoesNotExist as exc:  # noqa: PERF203 - 需要把可选项列出来给人看
    available = list(Flow.objects.values_list("slug", flat=True))
    raise SystemExit(
        f"找不到流程 {exc}。本版本可用流程：{available}"
    ) from exc

provider, created = OAuth2Provider.objects.get_or_create(name=NAME)
provider.client_id = client_id
provider.client_secret = client_secret
provider.client_type = "confidential"
provider.authorization_flow = authorization_flow
provider.invalidation_flow = invalidation_flow
# 应用的 ID Token 校验走本地 JWKS，不请求 userinfo，因此 claim 必须进 ID Token
provider.include_claims_in_id_token = True
provider.grant_types = ["authorization_code", "refresh_token"]
provider.redirect_uris = [
    RedirectURI(matching_mode=RedirectURIMatchingMode.STRICT, url=redirect_uri)
]
# 内置 scope 映射：不给的话 openid/profile/email 这些 scope 无人应答，ID Token 里没有 claim
provider.property_mappings.set(
    ScopeMapping.objects.filter(managed__startswith="goauthentik.io/providers/oauth2/scope-")
)
provider.save()

Application.objects.update_or_create(
    slug=NAME, defaults={"name": "MyAgentHarness", "provider": provider}
)

print(
    "BOOTSTRAP_OK created=%s redirect_uris=%s scopes=%d grant_types=%s"
    % (created, redirect_uri, provider.property_mappings.count(), provider.grant_types)
)
