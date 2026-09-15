"""积分结算 / 榜单快照任务（模块 B4）。

Redis 贡献榜 Sorted Set 每日落库快照；贡献分事件的事务化结算见 identity/growth。
"""


async def settle_scores(ctx: dict) -> None:
    # TODO(v0.5): rank:major:{id} / rank:course:{id} 每日快照落库
    raise NotImplementedError("结算任务尚未实现（v0.5 迭代交付）")
