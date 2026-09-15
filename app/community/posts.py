"""帖子服务（模块 B3）：Markdown + 图片，板块 qa / experience / bounty / discuss。

问答贴发帖后触发 ARQ 异步任务生成 AI 首答（基于本课程公共库，带引用溯源，
标注"AI 生成，仅供参考"），写入 posts.ai_first_answer 并通知提问者。
"""

# TODO(v0.5)
