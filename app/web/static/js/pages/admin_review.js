/* 管理员终审页交互（admin_review.html，v1.1 CSP 外置）。
 * finalVerdict —— 单任务终审裁决；coReviewBoard —— 等待协审队列（改派弹窗 / 直审）。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer）。
 * v1.1：统一 ZY.api；终审 / 改派 / 直审成功后 toast + 任务卡 / 队列行局部移除（消灭整页刷新）；
 * 改派弹窗接入 ZY.bindModalA11y（role=dialog / 焦点圈定 / Esc / 焦点还原）。 */
(function () {
  'use strict';

  function finalVerdict(taskId) {
    return {
      loading: false, error: '', result: '', comment: '',
      previewFailed: false, previewLoading: true,

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
          const data = await ZY.api.post('/api/admin/review/final', {
            task_id: taskId, verdict: verdict, comment: this.comment.trim() || null,
          });
          this.result = verdict === 'approve'
            ? '已通过终审并上架，投稿人获得 ' + data.score_granted + ' 贡献分'
            : '已驳回该投稿';
          ZY.toast(verdict === 'approve' ? '已通过终审并上架' : '已驳回该投稿', 'success');
          // 任务卡局部移除（淡出后摘除，免整页刷新）
          const card = this.$root;
          setTimeout(() => {
            card.classList.add('opacity-0', 'transition-opacity', 'duration-300');
            setTimeout(() => card.remove(), 320);
          }, 1200);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 等待协审队列行局部移除
  function removeCoTaskRow(taskId) {
    const row = document.getElementById('co-task-' + taskId);
    if (row) row.remove();
  }

  function coReviewBoard() {
    return {
      loading: false, _a11y: null,
      // 改派弹窗：搜索 → 选择两步
      reassignOpen: false, reassignTaskId: null,
      rq: '', candidates: null, searching: false, searchError: '',
      selected: [], opError: '',

      init() {
        const modal = document.getElementById('reassign-modal');
        if (!modal) return;
        this._a11y = ZY.bindModalA11y(modal, {
          isOpen: () => this.reassignOpen,
          onEscape: () => this.closeReassign(),
        });
        this.$watch('reassignOpen', () => { if (this._a11y) this._a11y.sync(); });
      },

      roleLabel(r) { return {student: '学生', reviewer: '协审员', builder: '共建者', admin: '管理员'}[r] || r; },
      isPicked(u) { return this.selected.some(x => x.id === u.id); },
      togglePick(u) {
        if (u.role !== 'reviewer') return;  // 服务端仅接受协审员，前端直接禁选
        const i = this.selected.findIndex(x => x.id === u.id);
        if (i >= 0) this.selected.splice(i, 1);
        else this.selected.push(u);
      },

      // 操作失败时经全局 confirmModal 弹出错误提示（setTimeout 避开 ask 与 confirm 的竞态）
      fail(e) {
        setTimeout(() => this.$dispatch('zy-ask', {
          title: '操作失败', message: e.message, confirmText: '知道了',
        }), 0);
      },

      openReassign(taskId) {
        this.reassignTaskId = taskId; this.reassignOpen = true;
        this.rq = ''; this.candidates = null; this.selected = [];
        this.searchError = ''; this.opError = '';
      },
      closeReassign() {
        if (this.loading) return;
        this.reassignOpen = false;
      },
      async searchReviewers() {
        this.searching = true; this.searchError = '';
        try {
          const q = this.rq.trim();
          const data = await ZY.api.get('/api/admin/users?size=20'
            + (q ? '&q=' + encodeURIComponent(q) : ''));
          this.candidates = data.items || [];
        } catch (e) { this.searchError = e.message; }
        finally { this.searching = false; }
      },
      async confirmReassign() {
        const body = {task_id: this.reassignTaskId};
        if (this.selected.length) body.reviewer_ids = this.selected.map(u => u.id);
        this.loading = true; this.opError = '';
        try {
          await ZY.api.post('/api/admin/review/reassign', body);
          const taskId = this.reassignTaskId;
          this.reassignOpen = false;
          removeCoTaskRow(taskId);
          ZY.toast('已改派协审员', 'success');
        } catch (e) { this.opError = e.message; }
        finally { this.loading = false; }
      },

      // 直审：approve 走确认弹窗，reject 走必填理由弹窗（全局 confirmModal）
      askDirect(taskId, verdict) {
        if (verdict === 'approve') {
          this.$dispatch('zy-ask', {
            title: '直审通过',
            message: '确认直审通过该投稿？将直接上架并结算贡献分。',
            confirmText: '直审通过',
            onConfirm: () => this.direct(taskId, 'approve', null),
          });
        } else {
          this.$dispatch('zy-ask', {
            title: '直审驳回',
            withInput: true, requireInput: true,
            placeholder: '请输入驳回理由（必填）',
            confirmText: '直审驳回',
            onConfirm: (comment) => this.direct(taskId, 'reject', comment),
          });
        }
      },
      async direct(taskId, verdict, comment) {
        this.loading = true;
        try {
          await ZY.api.post('/api/admin/review/direct',
                            {task_id: taskId, verdict: verdict, comment: comment});
          removeCoTaskRow(taskId);
          ZY.toast(verdict === 'approve' ? '已直审通过并上架' : '已直审驳回', 'success');
        } catch (e) { this.fail(e); }
        finally { this.loading = false; }
      },
    };
  }

  window.finalVerdict = finalVerdict;
  window.coReviewBoard = coReviewBoard;
})();
