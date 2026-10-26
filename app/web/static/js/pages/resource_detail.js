/* resource_detail.html 页面交互（CSP 外置，v1.1）。
 * favBox 为跨页共享组件，经 alpine:init 注册于 /static/js/components.js。
 * 加载方式：{% block head %} 内同步 <script src>（原因见 board.js 头注）。
 * Jinja 注入值经模板内 <script type="application/json" id="page-data"> 惰性数据块传递，
 * 这里在组件实例化时（Alpine 初始化后，DOM 已解析完）惰性读取。
 * v1.1：统一 ZY.api；克隆弹窗接入 ZY.bindModalA11y；克隆/评分成功 ZY.toast。 */
(function () {
  'use strict';

  function pageData() {
    return JSON.parse(document.getElementById('page-data').textContent);
  }



  // 克隆到个人库：modal 打开时懒加载本人个人课程列表
  window.cloneBox = function (resourceId) {
    return {
      cloneOpen: false, cloneCourseId: '', myCourses: [], coursesLoading: false,
      cloneLoading: false, cloneError: '', cloneDone: false, cloneMsg: '', _a11y: null,

      init() {
        // 弹窗 a11y：焦点圈定 / Esc 关闭 / 焦点还原（v1.1 F4）；非上架资源无弹窗节点，跳过
        const modal = document.getElementById('clone-modal');
        if (!modal) return;
        this._a11y = ZY.bindModalA11y(modal, {
          isOpen: () => this.cloneOpen,
          onEscape: () => { this.cloneOpen = false; },
        });
        this.$watch('cloneOpen', () => { if (this._a11y) this._a11y.sync(); });
      },

      async openClone() {
        this.cloneOpen = true; this.cloneError = ''; this.cloneDone = false;
        this.cloneCourseId = '';
        if (this.myCourses.length) return;
        this.coursesLoading = true;
        try {
          const data = await ZY.api.get('/api/library');
          const flat = [];
          for (const s of data.semesters || []) {
            for (const c of s.courses || []) flat.push({id: c.id, label: s.name + ' / ' + c.name});
          }
          for (const c of data.uncategorized_courses || []) flat.push({id: c.id, label: c.name});
          this.myCourses = flat;
        } catch (e) { this.cloneError = e.message; }
        finally { this.coursesLoading = false; }
      },

      async doClone() {
        this.cloneLoading = true; this.cloneError = '';
        try {
          const data = await ZY.api.post('/api/resources/' + resourceId + '/clone',
            {course_id: this.cloneCourseId ? Number(this.cloneCourseId) : null});
          this.cloneDone = true;
          this.cloneMsg = data.cloned
            ? '克隆成功，资料已进入你的个人库（解析完成后即可 AI 问答）'
            : '你的个人库中已有该资料，无需重复克隆';
          ZY.toast(data.cloned ? '克隆成功' : '个人库中已有该资料', 'success');
        } catch (e) { this.cloneError = e.message; }
        finally { this.cloneLoading = false; }
      },
    };
  };

  window.resourcePreview = function (url) {
    return {
      previewUrl: '', loading: false, failed: false, error: '', attempts: 0,
      loadPreview() {
        this.loading = true; this.failed = false; this.error = '';
        this.previewUrl = url + (url.includes('?') ? '&' : '?') + 'retry=' + (++this.attempts);
      },
      checkPreview(event) {
        this.loading = false;
        try {
          const doc = event.target.contentDocument;
          if (doc && doc.contentType === 'application/json') {
            this.failed = true;
            this.previewUrl = '';
            this.error = '预览仍在生成或暂时不可用，请稍后重试。';
          }
        } catch (e) { this.failed = false; }
      },
    };
  };

  window.resourceDownload = function (resourceId, cost, isSelf) {
    if (isSelf) cost = 0;
    return {
      loading: false, error: '',
      async requestDownload() {
        this.error = '';
        if (cost > 0) {
          this.loading = true;
          try {
            const me = await ZY.api.get('/api/users/me');
            const after = me.score - cost;
            if (after < 0) {
              this.error = '贡献分余额不足，还差 ' + (-after) + ' 分。';
              return;
            }
            this.$dispatch('zy-ask', {
              title: '确认付费下载',
              message: '本次下载消耗 ' + cost + ' 贡献分；当前余额 ' + me.score + ' 分，下载后余额 ' + after + ' 分。',
              confirmText: '确认下载', onConfirm: () => this.download(),
            });
          } catch (e) { this.error = e.message; }
          finally { this.loading = false; }
          return;
        }
        this.$dispatch('zy-ask', {
          title: '下载资料', message: '确认下载该资料原件？', confirmText: '下载',
          onConfirm: () => this.download(),
        });
      },
      async download() {
        if (this.loading) return;
        this.loading = true; this.error = '';
        try {
          const response = await fetch('/api/resources/' + resourceId + '/download', {
            credentials: 'same-origin', headers: {Accept: '*/*'},
          });
          if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            const detail = data.detail;
            throw new Error(typeof detail === 'string' ? detail : '下载失败（' + response.status + '），请稍后重试');
          }
          const blob = await response.blob();
          const disposition = response.headers.get('Content-Disposition') || '';
          const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
          const plain = disposition.match(/filename="?([^";]+)"?/i);
          const name = encoded ? decodeURIComponent(encoded[1]) : (plain ? plain[1] : '知屿资料');
          const href = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = href; link.download = name;
          document.body.appendChild(link); link.click(); link.remove();
          setTimeout(() => URL.revokeObjectURL(href), 1000);
          ZY.toast('下载已开始', 'success');
        } catch (e) { this.error = e.message || '下载失败，请稍后重试'; }
        finally { this.loading = false; }
      },
    };
  };

  window.ratingBox = function (resourceId, initial) {
    return {
      myRating: initial, hover: 0, loading: false, error: '',
      avg: pageData().ratingAvg,
      count: pageData().ratingCount,

      async rate(stars) {
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/resources/' + resourceId + '/rating', {stars: stars});
          this.myRating = data.my_rating;
          this.avg = data.rating.toFixed(2);
          this.count = data.rating_count;
          const line = document.getElementById('avg-line');
          if (line) {
            line.replaceChildren(ZY.ui.iconSvg('star-solid', 'h-4 w-4'));
            line.insertAdjacentText('beforeend', data.rating.toFixed(1));
            const count = document.createElement('span');
            count.className = 'text-xs font-normal text-muted';
            count.textContent = '（' + data.rating_count + ' 人）';
            line.appendChild(count);
          }
          ZY.toast('评分成功', 'success');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
