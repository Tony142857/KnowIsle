document.addEventListener('alpine:init', function () {
  'use strict';

  Alpine.data('searchPage', function () {
    let cfg = {};
    const dataEl = document.getElementById('page-data');
    if (dataEl) {
      try { cfg = JSON.parse(dataEl.textContent); } catch (e) { cfg = {}; }
    }
    const params = new URLSearchParams(window.location.search);
    return {
      q: cfg.q || '', submittedQuery: '', type: cfg.type || 'all',
      board: cfg.board || '', courseId: cfg.course_id ? String(cfg.course_id) : '',
      majorId: params.get('major_id') || '',
      posts: [], postsTotal: 0, postPage: 1, postLoading: false, postError: '', postRequestSeq: 0,
      resources: [], resourcesTotal: 0, resourcePage: 1, resourceLoading: false, resourceError: '', resourceRequestSeq: 0,
      failedPages: {post: null, resource: null},
      size: 10, loading: false, searched: false, error: '', requestSeq: 0,
      courseOptions: [], majorOptions: [], optionsError: '', optionsLoading: false,

      boardLabel(board) {
        return {qa: '知屿问答', discuss: '讨论区', experience: '经验长廊', bounty: '资料求援'}[board] || '其他板块';
      },
      typeLabel(type) { return {all: '全部类型', post: '仅帖子', resource: '仅资源'}[type] || '全部类型'; },
      relTime(iso) { return ZY.relTime(iso); },
      majorName() {
        const item = this.majorOptions.find(m => String(m.id) === String(this.majorId));
        return item ? item.name : '#' + this.majorId;
      },
      courseName() {
        const item = this.courseOptions.find(c => String(c.id) === String(this.courseId));
        return item ? item.name : '#' + this.courseId;
      },
      buildUrl(page, groupType) {
        const query = new URLSearchParams({
          q: this.submittedQuery, type: groupType || this.type,
          page: String(page), size: String(this.size),
        });
        if (this.board) query.set('board', this.board);
        if (this.courseId) query.set('course_id', this.courseId);
        if (this.majorId) query.set('major_id', this.majorId);
        return '/api/search?' + query;
      },
      syncUrl() {
        const query = new URLSearchParams();
        if (this.submittedQuery) query.set('q', this.submittedQuery);
        if (this.type !== 'all') query.set('type', this.type);
        if (this.board) query.set('board', this.board);
        if (this.courseId) query.set('course_id', this.courseId);
        if (this.majorId) query.set('major_id', this.majorId);
        window.history.pushState({}, '', '/search' + (query.toString() ? '?' + query : ''));
      },
      async loadOptions() {
        if (this.optionsLoading) return;
        this.optionsLoading = true; this.optionsError = '';
        const [courses, majors] = await Promise.allSettled([
          ZY.api.get('/api/courses?size=100'), ZY.api.get('/api/majors'),
        ]);
        const errors = [];
        if (courses.status === 'fulfilled') this.courseOptions = courses.value.items || [];
        else errors.push('公共课程');
        if (majors.status === 'fulfilled') this.majorOptions = majors.value.items || [];
        else errors.push('专业');
        if (errors.length) this.optionsError = errors.join('、') + '筛选项加载失败，请重试';
        this.optionsLoading = false;
      },
      async init() {
        this.loadOptions();
        if (this.q.trim()) {
          this.submittedQuery = this.q.trim();
          await this.runSearch(false);
        }
      },
      applyFilters() {
        if (!this.q.trim()) { this.error = '请输入搜索关键词'; return; }
        this.submittedQuery = this.q.trim();
        this.syncUrl();
        this.runSearch(false);
      },
      async runSearch() {
        const seq = ++this.requestSeq;
        this.postRequestSeq++; this.resourceRequestSeq++;
        this.postLoading = false; this.resourceLoading = false;
        this.loading = true; this.error = ''; this.searched = false;
        this.postError = ''; this.resourceError = '';
        this.failedPages = {post: null, resource: null};
        try {
          const data = await ZY.api.get(this.buildUrl(1));
          if (seq !== this.requestSeq) return;
          this.posts = (data.posts || {}).items || [];
          this.postsTotal = (data.posts || {}).total || 0;
          this.resources = (data.resources || {}).items || [];
          this.resourcesTotal = (data.resources || {}).total || 0;
          this.postPage = 1; this.resourcePage = 1; this.searched = true;
        } catch (e) { if (seq === this.requestSeq) this.error = e.message; }
        finally { if (seq === this.requestSeq) this.loading = false; }
      },
      async loadMore(group, retryPage) {
        const isPost = group === 'post';
        const loadingKey = isPost ? 'postLoading' : 'resourceLoading';
        const seqKey = isPost ? 'postRequestSeq' : 'resourceRequestSeq';
        const errorKey = isPost ? 'postError' : 'resourceError';
        if (this[loadingKey]) return;
        const page = retryPage || (isPost ? this.postPage : this.resourcePage) + 1;
        const seq = ++this[seqKey];
        const generation = this.requestSeq;
        this[loadingKey] = true; this[errorKey] = '';
        try {
          const data = await ZY.api.get(this.buildUrl(page, group));
          if (generation !== this.requestSeq || seq !== this[seqKey]) return;
          const result = (isPost ? data.posts : data.resources) || {};
          if (isPost) {
            this.posts = this.posts.concat(result.items || []);
            this.postsTotal = result.total || 0; this.postPage = page;
          } else {
            this.resources = this.resources.concat(result.items || []);
            this.resourcesTotal = result.total || 0; this.resourcePage = page;
          }
          this.failedPages[group] = null;
        } catch (e) {
          if (generation !== this.requestSeq || seq !== this[seqKey]) return;
          this.failedPages[group] = page;
          this[errorKey] = e.message || '加载失败，请重试';
        } finally {
          if (seq === this[seqKey]) this[loadingKey] = false;
        }
      },
      retryGroup(group) {
        const failed = this.failedPages[group];
        return this.loadMore(group, failed || undefined);
      },
      retry() {
        if (this.submittedQuery) this.runSearch();
        else this.applyFilters();
      },
      clearFilter(key) {
        this[key] = '';
        if (this.searched || this.submittedQuery) this.applyFilters();
      },
      clearAll() {
        this.requestSeq++; this.postRequestSeq++; this.resourceRequestSeq++;
        this.q = ''; this.submittedQuery = ''; this.type = 'all'; this.board = '';
        this.courseId = ''; this.majorId = ''; this.posts = []; this.resources = [];
        this.postsTotal = 0; this.resourcesTotal = 0; this.error = ''; this.searched = false;
        this.postError = ''; this.resourceError = ''; this.failedPages = {post: null, resource: null};
        this.loading = false; this.postLoading = false; this.resourceLoading = false;
        window.history.pushState({}, '', '/search');
        document.getElementById('search-query').focus();
      },
    };
  });
});
