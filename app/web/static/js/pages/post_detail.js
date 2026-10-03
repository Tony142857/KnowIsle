/* post_detail.html 页面交互（CSP 外置，v1.0）。
 * voteBox / favBox 为跨页共享组件，经 alpine:init 注册于 /static/js/components.js。
 * 加载方式：{% block head %} 内同步 <script src>（原因见 board.js 头注）。 */
(function () {
  'use strict';

  // 单条评论：点赞 + 楼内回复 + 采纳
  window.commentBox = function (commentId, score, myVote, loggedIn, canAccept) {
    return {
      score: score, myVote: myVote, loading: false, error: '',
      canAccept: canAccept,
      replyOpen: false, replyContent: '', replyError: '',
      async vote(value) {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          const resp = await fetch('/api/votes', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target_type: 'comment', target_id: commentId, value: value}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('操作失败 ' + resp.status));
          this.score = data.score; this.myVote = data.my_vote;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      async reply() {
        if (!this.replyContent.trim()) { this.replyError = '回复内容不能为空'; return; }
        this.loading = true; this.replyError = '';
        try {
          const resp = await fetch(window.location.pathname.replace('/posts/', '/api/posts/') + '/comments', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content: this.replyContent.trim(), parent_id: commentId}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('回复失败 ' + resp.status));
          window.location.reload();
        } catch (e) { this.replyError = e.message; }
        finally { this.loading = false; }
      },
      async accept() {
        this.loading = true; this.error = '';
        try {
          const resp = await fetch('/api/posts/comments/' + commentId + '/accept', {method: 'POST'});
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('采纳失败 ' + resp.status));
          window.location.reload();
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };

  // AI 首答 pending：每 3s 轮询帖子状态，done/failed 后停轮询并刷新页面
  window.aiPoll = function (postId) {
    return {
      timer: null,
      start() {
        this.timer = setInterval(async () => {
          try {
            const resp = await fetch('/api/posts/' + postId);
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.ai_answer_status !== 'pending') {
              clearInterval(this.timer);
              window.location.reload();
            }
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };

  // 经验帖 AI 摘要 pending：每 3s 轮询，摘要生成后刷新页面（v0.7）
  window.summaryPoll = function (postId) {
    return {
      timer: null,
      start() {
        this.timer = setInterval(async () => {
          try {
            const resp = await fetch('/api/posts/' + postId);
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.ai_summary_status !== 'pending') {
              clearInterval(this.timer);
              window.location.reload();
            }
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };

  // 精华标记开关（v0.7）：POST /api/admin/posts/{id}/feature（管理员/共建者）
  window.featureBox = function (postId, featured) {
    return {
      featured: featured, loading: false, error: '',
      async toggle() {
        this.loading = true; this.error = '';
        try {
          const resp = await fetch('/api/admin/posts/' + postId + '/feature', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({featured: !this.featured}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('操作失败 ' + resp.status));
          this.featured = data.featured;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
