/* 全站搜索（v0.8）：GET /api/search?q=&type=&board=&course_id=&page=&size=
 * 响应分组返回：{posts: {total, items: [...]}, resources: {total, items: [...]}}
 * 原 search.html 内联脚本外置（CSP 兼容）；查询条件由模板经 #page-data
 * （type=application/json 惰性数据块）注入，不再内联 Jinja 到脚本里。
 *
 * 加载时序：与 pages/login.js 相同——非 defer 在 head 解析期执行，早于 Alpine 启动。
 */
document.addEventListener('alpine:init', function () {
  'use strict';

  Alpine.data('searchPage', function () {
    // SSR 注入的查询条件（q / type / board / course_id），缺省与服务端路由默认值一致
    let cfg = {};
    const dataEl = document.getElementById('page-data');
    if (dataEl) {
      try { cfg = JSON.parse(dataEl.textContent); } catch (e) { cfg = {}; }
    }
    return {
      q: cfg.q || '', type: cfg.type || 'all',
      board: cfg.board || '', courseId: cfg.course_id || null,
      posts: [], postsTotal: 0, resources: [], resourcesTotal: 0,
      page: 1, size: 10,
      loading: false, searched: false, error: '',
      courseOptions: [],

      boardLabel(b) {
        return {qa: '知屿问答', discuss: '讨论区', experience: '经验长廊', bounty: '资料求援'}[b] || b;
      },
      get hasMore() {
        return this.posts.length < this.postsTotal || this.resources.length < this.resourcesTotal;
      },
      buildUrl(page) {
        const params = new URLSearchParams({
          q: this.q.trim(), type: this.type, page: String(page), size: String(this.size),
        });
        if (this.board) params.set('board', this.board);
        if (this.courseId) params.set('course_id', String(this.courseId));
        return '/api/search?' + params.toString();
      },
      // 课程下拉选项（GET /api/courses 公共课程列表）；失败静默降级为「全部课程」
      async loadCourses() {
        try {
          const resp = await fetch('/api/courses?size=100');
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) return;
          this.courseOptions = data.items || [];
        } catch (e) { /* 课程下拉加载失败不阻塞搜索 */ }
      },
      async init() {
        this.loadCourses();
        if (this.q.trim()) await this.load(1);
      },
      async load(page) {
        this.loading = true; this.error = '';
        try {
          const resp = await fetch(this.buildUrl(page));
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('搜索失败 ' + resp.status));
          const posts = data.posts || {};
          const resources = data.resources || {};
          if (page === 1) {
            this.posts = posts.items || [];
            this.resources = resources.items || [];
          } else {
            this.posts = this.posts.concat(posts.items || []);
            this.resources = this.resources.concat(resources.items || []);
          }
          this.postsTotal = posts.total || 0;
          this.resourcesTotal = resources.total || 0;
          this.page = page;
          this.searched = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  });
});
