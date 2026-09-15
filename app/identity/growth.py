"""成长激励（模块 B4 / §3.3）：贡献分、等级、信用分三线体系。

贡献分事件驱动结算：统一写 score_logs 明细 + users.score 事务更新 +
等级阈值检查 + Redis 贡献榜（rank:major:{id} / rank:course:{id}）Sorted Set 实时排名。
信用分：管理员裁决扣分（credit_logs 留痕），阈值触发限流/禁言/冻结阶梯处罚。
"""

# TODO(v0.5): grant_score / maybe_level_up / apply_credit_penalty
