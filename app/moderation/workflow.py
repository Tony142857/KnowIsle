"""三级审核状态机（模块 B2，状态流转见 §6.3）。

precheck → co_review（≥2 人多数决，48h 限时）→ final（管理员终审）→ done。
通过后：review_status → approved、document/chunk.scope → public、积分结算、
通知投稿人、上架课程空间。驳回必附理由，可修改重投，可申诉一次由另一名管理员复核。
"""

# TODO(v0.4): async def run_review_pipeline(resource_id: int)
