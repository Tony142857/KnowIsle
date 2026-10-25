(function () {
  'use strict';

  function decrementCount(id) {
    const el = document.getElementById(id);
    if (el) el.textContent = Math.max(0, (parseInt(el.textContent, 10) || 1) - 1) + ' 条';
  }

  function finalVerdict(taskId, previewUrl) {
    return {
      loading: false, submitted: false, error: '', result: '', comment: '', previewOpen: false,
      previewFailed: false, previewLoading: false, previewUrl: '', previewAttempts: 0, previewMessage: '',
      togglePreview() {
        this.previewOpen = !this.previewOpen;
        if (this.previewOpen && !this.previewUrl) this.loadPreview();
        if (!this.previewOpen) this.previewUrl = '';
      },
      loadPreview() {
        this.previewLoading = true; this.previewFailed = false;
        this.previewUrl = previewUrl + (previewUrl.includes('?') ? '&' : '?') + 'retry=' + (++this.previewAttempts);
      },
      checkPreview(event) {
        this.previewLoading = false;
        try {
          const doc = event.target.contentDocument;
          if (doc && doc.contentType === 'application/json') {
            const data = JSON.parse(doc.body.textContent || '{}');
            this.previewFailed = true; this.previewUrl = '';
            this.previewMessage = data.detail === '预览生成中' ? '预览仍在生成，请稍后重试。' : (data.detail || '预览加载失败，请重试。');
          }
        } catch (e) {
          this.previewFailed = true; this.previewUrl = ''; this.previewMessage = '预览加载失败，请重试。';
        }
      },
      submit(verdict) {
        if (this.loading || this.submitted) return;
        if (verdict === 'reject' && !this.comment.trim()) { this.error = '驳回必须填写理由'; return; }
        this.$dispatch('zy-ask', {
          title: verdict === 'approve' ? '确认终审通过' : '确认终审驳回',
          message: verdict === 'approve' ? '通过后资源将上架并结算贡献分，确认继续？' : '确认驳回该投稿？请核对驳回理由。',
          confirmText: verdict === 'approve' ? '终审通过' : '终审驳回', danger: verdict === 'reject',
          onConfirm: () => this.doSubmit(verdict),
        });
      },
      async doSubmit(verdict) {
        if (this.loading || this.submitted) return false;
        this.loading = true; this.error = ''; this.result = '';
        ZY.setBusy(this, true);
        try {
          const data = await ZY.api.post('/api/admin/review/final', {
            task_id: taskId, verdict: verdict, comment: this.comment.trim() || null,
          });
          this.submitted = true;
          this.result = verdict === 'approve' ? '已通过终审并上架，投稿人获得 ' + data.score_granted + ' 贡献分' : '已驳回该投稿';
          ZY.toast(verdict === 'approve' ? '已通过终审并上架' : '已驳回该投稿', 'success');
          decrementCount('final-queue-count');
          const card = this.$root;
          setTimeout(() => {
            card.classList.add('opacity-0', 'transition-opacity', 'duration-300');
            setTimeout(() => card.remove(), 320);
          }, 1200);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; ZY.setBusy(this, false); }
      },
    };
  }

  function removeCoTaskRow(taskId) {
    const row = document.getElementById('co-task-' + taskId);
    if (row) row.remove();
    decrementCount('co-queue-count');
  }

  function coReviewBoard() {
    return {
      loading: false, busyTask: null, _a11y: null, reassignOpen: false, reassignTaskId: null,
      rq: '', candidates: null, searching: false, searchError: '', selected: [], opError: '',
      init() {
        const modal = document.getElementById('reassign-modal');
        if (!modal) return;
        this._a11y = ZY.bindModalA11y(modal, {isOpen: () => this.reassignOpen, onEscape: () => this.closeReassign()});
        this.$watch('reassignOpen', () => { if (this._a11y) this._a11y.sync(); });
      },
      roleLabel(role) { return {student: '学生', reviewer: '协审员', builder: '共建者', admin: '管理员'}[role] || '未知角色'; },
      isPicked(user) { return this.selected.some(x => x.id === user.id); },
      togglePick(user) {
        if (user.role !== 'reviewer') return;
        const index = this.selected.findIndex(x => x.id === user.id);
        if (index >= 0) this.selected.splice(index, 1); else this.selected.push(user);
      },
      fail(error) {
        setTimeout(() => this.$dispatch('zy-ask', {title: '操作失败', message: error.message, confirmText: '知道了'}), 0);
      },
      openReassign(taskId) {
        this.reassignTaskId = taskId; this.reassignOpen = true; this.rq = '';
        this.candidates = null; this.selected = []; this.searchError = ''; this.opError = '';
      },
      closeReassign() { if (!this.loading) this.reassignOpen = false; },
      async searchReviewers() {
        this.searching = true; this.searchError = '';
        try {
          const q = this.rq.trim();
          const data = await ZY.api.get('/api/admin/users?size=20' + (q ? '&q=' + encodeURIComponent(q) : ''));
          this.candidates = data.items || [];
        } catch (e) { this.searchError = e.message; }
        finally { this.searching = false; }
      },
      async confirmReassign() {
        const body = {task_id: this.reassignTaskId};
        if (this.selected.length) body.reviewer_ids = this.selected.map(u => u.id);
        this.loading = true; this.opError = '';
        try {
          const data = await ZY.api.post('/api/admin/review/reassign', body);
          const taskId = this.reassignTaskId; this.reassignOpen = false;
          if (data.escalated) removeCoTaskRow(taskId);
          else {
            const row = document.getElementById('co-task-' + taskId);
            const state = row && row.querySelector('[data-slot="review-state"]');
            if (state) state.textContent = '等待协审 · ' + (data.assignee_ids || []).length + ' 人（已改派）';
          }
          ZY.toast(data.escalated ? '无人可派，任务已转入终审' : '已改派协审员', 'success');
        } catch (e) { this.opError = e.message; }
        finally { this.loading = false; }
      },
      askDirect(taskId, verdict) {
        if (this.busyTask !== null) return;
        this.$dispatch('zy-ask', {
          title: verdict === 'approve' ? '直审通过' : '直审驳回',
          message: verdict === 'approve' ? '确认直审通过该投稿？将直接上架并结算贡献分。' : '确认直审驳回该投稿？',
          withInput: verdict === 'reject', requireInput: verdict === 'reject',
          placeholder: '请输入驳回理由（必填）', confirmText: verdict === 'approve' ? '直审通过' : '直审驳回',
          danger: verdict === 'reject', onConfirm: comment => this.direct(taskId, verdict, verdict === 'reject' ? comment : null),
        });
      },
      async direct(taskId, verdict, comment) {
        if (this.busyTask !== null) return;
        this.busyTask = taskId;
        try {
          await ZY.api.post('/api/admin/review/direct', {task_id: taskId, verdict: verdict, comment: comment});
          removeCoTaskRow(taskId);
          ZY.toast(verdict === 'approve' ? '已直审通过并上架' : '已直审驳回', 'success');
        } catch (e) { this.fail(e); }
        finally { this.busyTask = null; }
      },
    };
  }

  window.finalVerdict = finalVerdict;
  window.coReviewBoard = coReviewBoard;
})();
