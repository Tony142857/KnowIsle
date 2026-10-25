(function () {
  'use strict';

  function coVerdict(taskId, previewUrl) {
    return {
      loading: false, submitted: false, error: '', result: '', comment: '',
      previewFailed: false, previewLoading: false, previewUrl: '', previewMessage: '',
      previewAttempts: 0, previewTimer: null,
      init() { this.loadPreview(); },
      loadPreview() {
        clearTimeout(this.previewTimer);
        this.previewLoading = true; this.previewFailed = false; this.previewMessage = '';
        this.previewUrl = previewUrl + (previewUrl.includes('?') ? '&' : '?') + 'retry=' + (++this.previewAttempts);
      },
      schedulePreview() {
        this.previewLoading = false; this.previewFailed = true; this.previewUrl = '';
        const delay = Math.min(30000, 2000 * Math.pow(2, Math.min(4, this.previewAttempts - 1)));
        this.previewMessage = '预览仍在生成，将在 ' + Math.ceil(delay / 1000) + ' 秒后自动重试。';
        this.previewTimer = setTimeout(() => this.loadPreview(), delay);
      },
      checkPreview(event) {
        this.previewLoading = false;
        try {
          const doc = event.target.contentDocument;
          if (doc && doc.contentType === 'application/json') {
            const data = JSON.parse(doc.body.textContent || '{}');
            if (data.detail === '预览生成中') this.schedulePreview();
            else {
              clearTimeout(this.previewTimer);
              this.previewFailed = true; this.previewUrl = '';
              this.previewMessage = data.detail || '预览加载失败，请重试';
            }
          } else this.previewAttempts = 0;
        } catch (e) {
          this.previewFailed = true; this.previewUrl = '';
          this.previewMessage = '预览加载失败，请重试';
        }
      },
      submit(verdict) {
        if (this.loading || this.submitted) return;
        if (verdict === 'reject' && !this.comment.trim()) { this.error = '驳回必须填写理由'; return; }
        this.$dispatch('zy-ask', {
          title: verdict === 'approve' ? '确认协审通过' : '确认协审驳回',
          message: verdict === 'approve' ? '确认通过该投稿的协审？提交后不能修改意见。' : '确认驳回该投稿？请核对已填写驳回理由。',
          confirmText: verdict === 'approve' ? '确认通过' : '确认驳回', danger: verdict === 'reject',
          onConfirm: () => this.doSubmit(verdict),
        });
      },
      async doSubmit(verdict) {
        if (this.loading || this.submitted) return false;
        this.loading = true; this.error = ''; this.result = '';
        ZY.setBusy(this, true);
        try {
          const data = await ZY.api.post('/api/review/tasks/' + taskId + '/verdict', {
            verdict: verdict, comment: this.comment.trim() || null,
          });
          this.submitted = true;
          this.result = {recorded: '意见已记录，等待其他协审员', advanced_final: '已通过协审，进入管理员终审', rejected: '该投稿已被驳回'}[data.status] || '已提交';
          ZY.toast('协审意见已提交', 'success');
          let nextHref = '/review';
          const link = document.getElementById('task-link-' + taskId);
          if (link) {
            const li = link.closest('li');
            const sibling = li && (li.nextElementSibling || li.previousElementSibling);
            const next = sibling && sibling.querySelector('a');
            if (next) nextHref = next.getAttribute('href');
            if (li) li.remove();
          }
          const count = document.getElementById('review-queue-count');
          if (count) count.textContent = Math.max(0, (parseInt(count.textContent, 10) || 1) - 1) + ' 条';
          setTimeout(() => { window.location.href = nextHref; }, 1200);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; ZY.setBusy(this, false); }
      },
    };
  }

  window.coVerdict = coVerdict;
})();
