/* 协审工作台交互（review.html，v1.0 CSP 外置）。
 * coVerdict —— 预览加载检测 + 协审裁决提交（通过 / 驳回）。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer）。 */
(function () {
  'use strict';

  function coVerdict(taskId) {
    return {
      loading: false, error: '', result: '', comment: '', previewFailed: false,

      // 同源 iframe：409 时后端返回 JSON 错误体，contentType 为 application/json
      checkPreview(event) {
        try {
          const doc = event.target.contentDocument;
          if (doc && doc.contentType === 'application/json') this.previewFailed = true;
        } catch (e) { /* 跨域或尚未加载时忽略 */ }
      },

      async submit(verdict) {
        if (verdict === 'reject' && !this.comment.trim()) {
          this.error = '驳回必须填写理由'; return;
        }
        this.loading = true; this.error = ''; this.result = '';
        try {
          const resp = await fetch('/api/review/tasks/' + taskId + '/verdict', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({verdict: verdict, comment: this.comment.trim() || null}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('提交失败 ' + resp.status));
          this.result = {
            recorded: '意见已记录，等待其他协审员',
            advanced_final: '已通过协审，进入管理员终审',
            rejected: '该投稿已被驳回',
          }[data.status] || '已提交';
          setTimeout(() => window.location.href = '/review', 1200);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  window.coVerdict = coVerdict;
})();
