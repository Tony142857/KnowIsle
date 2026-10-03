/* resource_detail.html 页面交互（CSP 外置，v1.0）。
 * favBox 为跨页共享组件，经 alpine:init 注册于 /static/js/components.js。
 * 加载方式：{% block head %} 内同步 <script src>（原因见 board.js 头注）。
 * Jinja 注入值经模板内 <script type="application/json" id="page-data"> 惰性数据块传递，
 * 这里在组件实例化时（Alpine 初始化后，DOM 已解析完）惰性读取。 */
(function () {
  'use strict';

  function pageData() {
    return JSON.parse(document.getElementById('page-data').textContent);
  }

  // 克隆到个人库：modal 打开时懒加载本人个人课程列表
  window.cloneBox = function (resourceId) {
    return {
      cloneOpen: false, cloneCourseId: '', myCourses: [],
      cloneLoading: false, cloneError: '', cloneDone: false, cloneMsg: '',

      async openClone() {
        this.cloneOpen = true; this.cloneError = ''; this.cloneDone = false;
        this.cloneCourseId = '';
        if (this.myCourses.length) return;
        try {
          const resp = await fetch('/api/library');
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载个人课程失败 ' + resp.status));
          const flat = [];
          for (const s of data.semesters || []) {
            for (const c of s.courses || []) flat.push({id: c.id, label: s.name + ' / ' + c.name});
          }
          for (const c of data.uncategorized_courses || []) flat.push({id: c.id, label: c.name});
          this.myCourses = flat;
        } catch (e) { this.cloneError = e.message; }
      },

      async doClone() {
        this.cloneLoading = true; this.cloneError = '';
        try {
          const resp = await fetch('/api/resources/' + resourceId + '/clone', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({course_id: this.cloneCourseId ? Number(this.cloneCourseId) : null}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('克隆失败 ' + resp.status));
          this.cloneDone = true;
          this.cloneMsg = data.cloned
            ? '克隆成功，资料已进入你的个人库（解析完成后即可 AI 问答）'
            : '你的个人库中已有该资料，无需重复克隆';
        } catch (e) { this.cloneError = e.message; }
        finally { this.cloneLoading = false; }
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
          const resp = await fetch('/api/resources/' + resourceId + '/rating', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({stars: stars}),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('评分失败 ' + resp.status));
          this.myRating = data.my_rating;
          this.avg = data.rating.toFixed(2);
          this.count = data.rating_count;
          // 同步刷新侧栏服务端渲染的「综合评分」行
          const line = document.getElementById('avg-line');
          if (line) line.innerHTML = '★ ' + data.rating.toFixed(1)
            + ' <span class="text-xs font-normal text-slate-400">（'
            + data.rating_count + ' 人）</span>';
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
