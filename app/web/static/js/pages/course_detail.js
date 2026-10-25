(function () {
  'use strict';

  function courseFollow(courseId, following, count, loggedIn) {
    return {
      following: following, count: count, loading: false, error: '',
      async toggle() {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          await ZY.api.request(this.following ? 'DELETE' : 'POST', '/api/follows', {
            target_type: 'course', target_id: courseId,
          });
          this.following = !this.following;
          this.count += this.following ? 1 : -1;
          ZY.toast(this.following ? '已关注本课程' : '已取消关注');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  function courseTabs(courseId, loggedIn) {
    const tabs = ['resources', 'chat', 'discuss', 'rank'];
    return {
      tab: 'resources', resources: [], total: 0, page: 1, size: 12, query: '',
      loading: false, loaded: false, error: '', needLogin: !loggedIn,
      selectedChapter: null, selectedChapterTitle: '', requestSeq: 0, searchTimer: null,
      rankItems: [], rankLoading: false, rankLoaded: false, rankError: '',

      init() {
        this.readUrl();
        window.addEventListener('popstate', () => {
          this.readUrl();
          this.activateTab(false);
        });
        this.activateTab(false);
      },
      readUrl() {
        const params = new URLSearchParams(window.location.search);
        this.tab = tabs.includes(params.get('tab')) ? params.get('tab') : 'resources';
        this.query = params.get('q') || '';
        this.page = Math.max(1, Number(params.get('page')) || 1);
        const chapter = Number(params.get('chapter_id'));
        this.selectedChapter = chapter > 0 ? chapter : null;
        this.updateChapterTitle();
      },
      syncUrl(push) {
        const params = new URLSearchParams(window.location.search);
        if (this.tab === 'resources') params.delete('tab'); else params.set('tab', this.tab);
        if (this.query.trim()) params.set('q', this.query.trim()); else params.delete('q');
        if (this.selectedChapter !== null) params.set('chapter_id', this.selectedChapter);
        else params.delete('chapter_id');
        if (this.page > 1 && this.tab === 'resources') params.set('page', this.page);
        else params.delete('page');
        const url = window.location.pathname + (params.toString() ? '?' + params : '');
        window.history[push ? 'pushState' : 'replaceState']({tab: this.tab}, '', url);
      },
      setTab(tab) {
        if (!tabs.includes(tab) || tab === this.tab) return;
        this.tab = tab;
        this.syncUrl(true);
        this.activateTab(false);
      },
      activateTab(push) {
        this.syncUrl(push);
        if (this.tab === 'resources') this.loadResources();
        if (this.tab === 'rank' && !this.rankLoaded && !this.rankLoading) this.loadRank();
      },
      updateChapterTitle() {
        this.selectedChapterTitle = '';
        if (this.selectedChapter === null) return;
        const item = document.querySelector('li[data-chapter-id="' + this.selectedChapter + '"] [data-chapter-title]');
        this.selectedChapterTitle = item ? item.dataset.chapterTitle : '章节 #' + this.selectedChapter;
      },
      selectChapter(id) {
        this.selectedChapter = this.selectedChapter === id ? null : id;
        this.page = 1;
        this.updateChapterTitle();
        this.tab = 'resources';
        this.syncUrl(true);
        if (!this.needLogin) this.loadResources();
      },
      clearChapter() {
        this.selectedChapter = null;
        this.selectedChapterTitle = '';
        this.page = 1;
        this.syncUrl(true);
        this.loadResources();
      },
      clearResourceFilters() {
        this.query = '';
        this.selectedChapter = null;
        this.selectedChapterTitle = '';
        this.page = 1;
        this.syncUrl(true);
        this.loadResources();
      },
      scheduleSearch() {
        clearTimeout(this.searchTimer);
        this.searchTimer = setTimeout(() => this.searchResources(), 320);
      },
      searchResources() {
        clearTimeout(this.searchTimer);
        this.page = 1;
        this.syncUrl(true);
        this.loadResources();
      },
      goPage(page) {
        if (page < 1 || (page - 1) * this.size >= this.total) return;
        this.page = page;
        this.syncUrl(true);
        this.loadResources();
      },
      async loadResources() {
        if (this.needLogin) { this.loaded = true; return; }
        const seq = ++this.requestSeq;
        this.loading = true; this.error = '';
        try {
          const params = new URLSearchParams({course_id: String(courseId), page: String(this.page), size: String(this.size)});
          if (this.selectedChapter !== null) params.set('chapter_id', this.selectedChapter);
          if (this.query.trim()) params.set('q', this.query.trim());
          const data = await ZY.api.get('/api/resources?' + params);
          if (seq !== this.requestSeq) return;
          this.resources = data.items || [];
          this.total = data.total || 0;
          this.loaded = true;
          if (!this.resources.length && this.total && this.page > 1) this.goPage(Math.ceil(this.total / this.size));
        } catch (e) {
          if (seq === this.requestSeq) this.error = e.message;
        } finally {
          if (seq === this.requestSeq) this.loading = false;
        }
      },
      async loadRank() {
        this.rankLoading = true; this.rankError = '';
        try {
          const data = await ZY.api.get('/api/courses/' + courseId + '/leaderboard?limit=20');
          this.rankItems = data.items || [];
          this.rankLoaded = true;
        } catch (e) { this.rankError = e.message; }
        finally { this.rankLoading = false; }
      },
    };
  }

  function insertChapterNode(ch) {
    const tree = document.getElementById('chapter-tree');
    if (!tree) return;
    const empty = document.getElementById('chapter-tree-empty');
    if (empty) empty.classList.add('hidden');
    const li = document.createElement('li');
    li.setAttribute('x-data', '{ open: true }');
    li.setAttribute('data-chapter-id', ch.id);
    li.className = 'animate-fade-up';
    const row = document.createElement('div');
    row.className = 'flex items-center gap-0.5';
    const spacer = document.createElement('span');
    spacer.className = 'w-4 shrink-0';
    const label = document.createElement('button');
    label.type = 'button';
    label.dataset.chapterTitle = ch.title;
    label.setAttribute(':aria-pressed', 'selectedChapter === ' + ch.id);
    label.className = 'block min-w-0 flex-1 truncate rounded px-2 py-1 text-left text-sm transition-colors';
    label.setAttribute(':class', 'selectedChapter === ' + ch.id +
      " ? 'bg-brand-50 text-brand-700 font-medium ring-1 ring-brand-200' : 'text-slate-600 hover:bg-slate-100'");
    label.setAttribute('@click', 'selectChapter(' + ch.id + ')');
    label.textContent = ch.title;
    row.append(spacer, label);
    li.appendChild(row);
    if (ch.parent_id) {
      const parent = tree.querySelector('li[data-chapter-id="' + ch.parent_id + '"]');
      if (parent) {
        let ul = parent.querySelector(':scope > ul');
        if (!ul) {
          ul = document.createElement('ul');
          ul.className = 'ml-3 border-l border-slate-200 pl-2';
          parent.appendChild(ul);
        }
        ul.appendChild(li);
        return;
      }
    }
    tree.appendChild(li);
  }

  function chapterAdder(courseId) {
    return {
      title: '', parent_id: '', loading: false, error: '',
      async submit() {
        if (!this.title.trim()) { this.error = '请填写章节标题'; return; }
        this.loading = true; this.error = '';
        try {
          const ch = await ZY.api.post('/api/courses/' + courseId + '/chapters', {
            title: this.title.trim(), parent_id: this.parent_id ? Number(this.parent_id) : null,
          });
          insertChapterNode(ch);
          if (!ch.parent_id) {
            const select = document.getElementById('chapter-parent-select');
            if (select) {
              const opt = document.createElement('option');
              opt.value = ch.id;
              opt.textContent = '归于「' + ch.title + '」下';
              select.appendChild(opt);
            }
          }
          this.title = ''; this.parent_id = '';
          ZY.toast('章节已添加');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  window.courseFollow = courseFollow;
  window.courseTabs = courseTabs;
  window.chapterAdder = chapterAdder;
})();
