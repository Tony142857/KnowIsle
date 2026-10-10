/* 课程空间详情页交互（course_detail.html，v1.1 翡翠绿·海岛改版）。
 * courseFollow —— 关注课程开关；courseTabs —— Tab 懒加载 + 章节树联动过滤；
 * chapterAdder —— 添加章节（原模板内联 x-data 外置）：成功后客户端插入章节树节点
 * + ZY.toast 反馈，消灭整页 reload。
 * 全部请求走 ZY.api（api.js）；匿名用户不拉取资料列表（/api/resources 需登录），
 * 展示登录引导卡代替原 401 文案。
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
    return {
      tab: 'resources',
      resources: [], loading: false, loaded: false, error: '',
      needLogin: !loggedIn,
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
        if (this.needLogin) return;
        this.loadResources();
      },

      async loadResources() {
        // /api/resources 需登录：匿名展示登录引导卡（ZY.api 对 401 一律跳 /login，
        // 这里前置拦截避免匿名浏览课程页被重定向）
        if (this.needLogin) { this.loaded = true; return; }
        this.loading = true; this.error = '';
        try {
          let url = '/api/resources?course_id=' + courseId + '&size=50';
          if (this.selectedChapter !== null) url += '&chapter_id=' + this.selectedChapter;
          const data = await ZY.api.get(url);
          this.resources = data.items || [];
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
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

  // 添加章节成功：客户端插入章节树节点（结构与 chapter_node 宏一致，
  // Alpine 3 经 MutationObserver 自动初始化新增节点上的 x-data/@click 绑定）
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
    const label = document.createElement('span');
    label.className = 'block min-w-0 flex-1 cursor-pointer truncate rounded px-2 py-1 text-sm transition-colors';
    label.setAttribute(':class', 'selectedChapter === ' + ch.id +
      " ? 'bg-brand-50 text-brand-700 font-medium ring-1 ring-brand-200' : 'text-slate-600 hover:bg-slate-100'");
    label.setAttribute('@click', 'selectChapter(' + ch.id + ')');
    label.textContent = ch.title;
    row.appendChild(spacer);
    row.appendChild(label);
    li.appendChild(row);

    if (ch.parent_id) {
      const parent = tree.querySelector('li[data-chapter-id="' + ch.parent_id + '"]');
      if (parent) {
        // 父节点已有子列表则追加；没有则补一个恒展开的子列表（无折叠按钮，直接可见）
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

  // 添加章节（共建者/管理员）：POST /api/courses/{id}/chapters，
  // 成功局部更新章节树与父章节下拉 + toast，失败内联错误
  function chapterAdder(courseId) {
    return {
      title: '', parent_id: '', loading: false, error: '',
      async submit() {
        if (!this.title.trim()) { this.error = '请填写章节标题'; return; }
        this.loading = true; this.error = '';
        try {
          const ch = await ZY.api.post('/api/courses/' + courseId + '/chapters', {
            title: this.title.trim(),
            parent_id: this.parent_id ? Number(this.parent_id) : null,
          });
          insertChapterNode(ch);
          // 根章节同步补进父章节下拉（仅根节点可作父级，与模板 SSR 口径一致）
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
