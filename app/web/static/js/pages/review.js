/* 协审工作台交互（review.html，v1.1 CSP 外置）。
 * coVerdict —— 预览加载检测（骨架屏 + 409 识别）+ 协审裁决提交（通过 / 驳回）。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer）。
 * v1.1：统一 ZY.api；裁决成功 toast + 左侧队列局部移除该任务 + 延迟导航下一任务（消灭整页刷新）。 */
(function () {
  'use strict';

  function coVerdict(taskId) {
    return {
      loading: false, error: '', result: '', comment: '',
      previewFailed: false, previewLoading: true,

      // 同源 iframe：409 时后端返回 JSON 错误体，contentType 为 application/json
      checkPreview(event) {
        this.previewLoading = false;
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
          const data = await ZY.api.post('/api/review/tasks/' + taskId + '/verdict', {
            verdict: verdict, comment: this.comment.trim() || null,
          });
          this.result = {
            recorded: '意见已记录，等待其他协审员',
            advanced_final: '已通过协审，进入管理员终审',
            rejected: '该投稿已被驳回',
          }[data.status] || '已提交';
          ZY.toast('协审意见已提交', 'success');
          // 左侧队列局部移除本任务，随后导航到下一任务（无剩余则回队列页）
          let nextHref = '/review';
          const link = document.getElementById('task-link-' + taskId);
          if (link) {
            const li = link.closest('li');
            const sib = (li && (li.nextElementSibling || li.previousElementSibling))
              ? (li.nextElementSibling || li.previousElementSibling).querySelector('a') : null;
            if (sib) nextHref = sib.getAttribute('href');
            if (li) li.remove();
          }
          setTimeout(() => { window.location.href = nextHref; }, 1200);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  window.coVerdict = coVerdict;
})();
