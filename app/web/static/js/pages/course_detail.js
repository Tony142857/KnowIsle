/* 课程空间详情页交互（course_detail.html，v1.0 CSP 外置）。
 * courseFollow —— 关注课程开关；courseTabs —— Tab 懒加载 + 章节树联动过滤。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer，
 * 实测 defer 脚本晚于 alpine 的 queueMicrotask 启动，x-data 会找不到组件）。 */
(function () {
  'use strict';

  // 关注课程开关（v0.6）：POST/DELETE /api/follows（target_type=course），未登录跳 /login
  function courseFollow(courseId, following, count, loggedIn) {
    return {
      following: following, count: count, loading: false, error: '',
      async toggle() {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          const resp = await fetch('/api/follows', {
            method: this.following ? 'DELETE' : 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target_type: 'course', target_id: courseId}),
          });
          if (resp.status !== 204) {
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || ('操作失败 ' + resp.status));
          }
          this.following = !this.following;
          this.count += this.following ? 1 : -1;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  function courseTabs(courseId) {
    return {
      tab: 'resources',
      resources: [], loading: false, loaded: false, error: '',
      selectedChapter: null,
      rankItems: [], rankLoading: false, rankLoaded: false, rankError: '',

      init() {
        // Tab 首次激活时懒加载（resources 为默认 Tab，首屏即触发）
        this.$watch('tab', (value) => {
          if (value === 'resources' && !this.loaded) this.loadResources();
          if (value === 'rank' && !this.rankLoaded) this.loadRank();
        });
        this.loadResources();
      },

      // 章节树点击：选中/再点取消，联动「资料库」Tab 按 chapter_id 重新拉取
      selectChapter(id) {
        this.selectedChapter = this.selectedChapter === id ? null : id;
        this.loadResources();
      },

      async loadResources() {
        this.loading = true; this.error = '';
        try {
          let url = '/api/resources?course_id=' + courseId + '&size=50';
          if (this.selectedChapter !== null) url += '&chapter_id=' + this.selectedChapter;
          const resp = await fetch(url);
          const data = await resp.json().catch(() => ({}));
          if (resp.status === 401) throw new Error('登录后可查看课程资料库');
          if (!resp.ok) throw new Error(data.detail || ('加载失败 ' + resp.status));
          this.resources = data.items || [];
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },

      async loadRank() {
        this.rankLoading = true; this.rankError = '';
        try {
          const resp = await fetch('/api/courses/' + courseId + '/leaderboard?limit=20');
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载失败 ' + resp.status));
          this.rankItems = data.items || [];
          this.rankLoaded = true;
        } catch (e) { this.rankError = e.message; }
        finally { this.rankLoading = false; }
      },
    };
  }

  window.courseFollow = courseFollow;
  window.courseTabs = courseTabs;
})();
