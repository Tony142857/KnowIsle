/* 个人知识库页（library.html）交互逻辑（v1.1 翡翠绿·海岛改版）。
 * 模板 head 块内同步加载（勿加 defer）：须在 defer 的 Alpine 启动前定义全局工厂函数。
 *
 * v1.1 变更：
 * - JSON 请求统一走 ZY.api（消灭裸 fetch；文件上传为 multipart，ZY.api 仅支持 JSON，
 *   上传保留 fetch 并附 shimmer 进度条与按钮禁用反馈）；
 * - 消灭整页 location.reload()：新建学期/课程/章节成功改为客户端局部插入 + ZY.toast；
 * - 投稿 modal 接入 ZY.bindModalA11y（role=dialog/焦点圈定/Esc/焦点还原），
 *   成功 toast + 行内「审核中」徽章局部更新（submitted 映射）；
 * - 文档列表时间在 init 时经 ZY.relTime 转为相对格式（[data-rel-time] 挂载点）。 */
(function () {
  'use strict';

  // 学期分组容器 / 新建课程学期下拉 / 未分组网格的 DOM id 约定（与 library.html 对齐）
  function semesterGroupsEl() { return document.getElementById('semester-groups'); }

  function hideLibraryEmpty() {
    const el = document.getElementById('library-empty');
    if (el) el.classList.add('hidden');
  }

  // 新建学期成功：追加学期分组 section（含空课程网格），并同步新建课程表单下拉
  function appendSemesterSection(s) {
    hideLibraryEmpty();
    const host = semesterGroupsEl();
    if (host) {
      const section = document.createElement('section');
      section.className = 'mb-8 animate-fade-up';
      section.id = 'sem-section-' + s.id;
      const h2 = document.createElement('h2');
      h2.className = 'mb-3 flex items-center gap-2 text-lg font-semibold text-slate-700';
      h2.textContent = s.name;
      const count = document.createElement('span');
      count.className = 'text-xs font-normal text-slate-400';
      count.textContent = '0 门课程';
      h2.appendChild(count);
      const grid = document.createElement('div');
      grid.className = 'grid items-start gap-4 md:grid-cols-2';
      grid.id = 'course-grid-' + s.id;
      section.appendChild(h2);
      section.appendChild(grid);
      host.appendChild(section);
    }
    const select = document.getElementById('course-semester-select');
    if (select) {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name;
      select.appendChild(opt);
    }
  }

  // 新建课程成功：向目标学期/未分组网格追加课程卡片。
  // 卡片含 Alpine 绑定（添加章节/上传资料），Alpine 3 经 MutationObserver 自动初始化
  // 新增 DOM 上的 x-data/@click；结构与模板 course_card 宏保持一致。
  function appendCourseCard(c) {
    hideLibraryEmpty();
    const gridId = c.semester_id ? 'course-grid-' + c.semester_id : 'course-grid-none';
    let grid = document.getElementById(gridId);
    if (!grid && c.semester_id) return; // 目标学期 section 不在本页（理论不发生）
    if (!grid) return;
    const uncat = document.getElementById('sem-section-none');
    if (!c.semester_id && uncat) uncat.classList.remove('hidden');

    const card = document.createElement('div');
    card.className = 'card card-hover animate-fade-up p-4';
    card.innerHTML =
      '<div class="flex flex-wrap items-center gap-3">' +
        '<h3 class="text-card-title text-slate-800" data-role="name"></h3>' +
        '<span class="text-xs text-slate-400">0 份资料</span>' +
        '<div class="ml-auto flex flex-wrap items-center gap-2 text-sm">' +
          '<a href="/library/chat?course_id=' + c.id + '" class="text-brand-600 transition-colors hover:text-brand-700 hover:underline">AI 问答</a>' +
          '<a href="/library/review?course_id=' + c.id + '" class="text-brand-600 transition-colors hover:text-brand-700 hover:underline">复习工具</a>' +
          '<button type="button" @click="chapterFormFor = chapterFormFor === ' + c.id + ' ? null : ' + c.id + '; chapterTitle = \'\'"' +
                  ' class="btn btn-ghost !px-3 !py-1.5 text-xs">添加章节</button>' +
          '<button type="button" @click="uploadFormFor = uploadFormFor === ' + c.id + ' ? null : ' + c.id + '; uploadFile = null"' +
                  ' class="btn btn-ghost !px-3 !py-1.5 text-xs">上传资料</button>' +
        '</div>' +
      '</div>' +
      '<div id="chapter-list-' + c.id + '" class="mt-2 flex flex-wrap items-center gap-1.5"></div>' +
      '<div x-show="chapterFormFor === ' + c.id + '" x-cloak' +
           ' class="mt-3 flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3">' +
        '<input type="text" x-model="chapterTitle" placeholder="章节标题" class="input-base flex-1 !py-1.5">' +
        '<button type="button" @click="createChapter(' + c.id + ')" :disabled="loading"' +
                ' class="btn btn-primary !px-3 !py-1.5">添加</button>' +
      '</div>' +
      '<div x-show="uploadFormFor === ' + c.id + '" x-cloak' +
           ' class="mt-3 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">' +
        '<div class="flex flex-wrap items-center gap-2">' +
          '<input type="file" @change="uploadFile = $event.target.files[0] || null"' +
                 ' class="text-sm text-slate-500 file:mr-2 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-1.5 file:text-sm file:text-brand-700 file:transition-colors hover:file:bg-brand-100">' +
          '<button type="button" @click="upload(' + c.id + ')" :disabled="loading"' +
                  ' class="btn btn-primary !px-3 !py-1.5">上传</button>' +
        '</div>' +
        '<p class="text-xs text-slate-400">支持 PDF 教材 / PPT / Word / Markdown，上传后自动解析入库。</p>' +
      '</div>' +
      '<p class="mt-3 text-xs text-slate-400">暂无资料。点击「上传资料」导入课件 / 教材，解析完成后即可向 AI 提问。</p>';
    card.querySelector('[data-role="name"]').textContent = c.name;
    grid.appendChild(card);
  }

  // 章节徽章内的 chevron 图标（与 library.html course_card 宏的 icon('chevron-right') 输出一致）
  var CHEVRON_SVG = '<svg class="w-3 h-3 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M9 6l6 6-6 6"/></svg>';

  // 新建章节成功：徽章插入课程章节区（章节仅作展示，上传不再支持人工挂载——v1.1 起 chapter_id 由解析自动构建）
  function appendChapterBadge(courseId, ch) {
    const list = document.getElementById('chapter-list-' + courseId);
    if (list) {
      const badge = document.createElement('span');
      badge.className = 'badge badge-slate animate-fade-up';
      badge.textContent = ch.title;
      badge.insertAdjacentHTML('beforeend', CHEVRON_SVG);
      list.appendChild(badge);
    }
  }

  window.libraryPage = function () {
    return {
      loading: false, error: '',
      showSemesterForm: false, showCourseForm: false,
      semesterName: '', courseName: '', courseSemesterId: '', courseDesc: '',
      chapterFormFor: null, chapterTitle: '',
      uploadFormFor: null, uploadFile: null, uploading: false,
      // 投稿 modal 状态
      submitOpen: false, submitDocId: null, submitTitle: '', submitDesc: '',
      submitCourseId: '', submitChapterId: '', submitChapters: [],
      submitError: '', courses: [],
      // 本次会话内已成功投稿的文档：docId -> true（行内「审核中」徽章局部更新用）
      submitted: {},
      // 上传后待解析的文档：courseId -> [{id, file_name, status, status_url}]
      pending: {},

      init() {
        // SSR 时间统一转相对格式（[data-rel-time] 挂载点）
        document.querySelectorAll('[data-rel-time]').forEach(function (el) {
          const text = ZY.relTime(el.getAttribute('data-rel-time'));
          if (text) el.textContent = text;
        });
        // 投稿 modal 可访问性：焦点圈定 / Esc 关闭 / 焦点还原
        // （$nextTick 等待子树初始化完成，确保 x-ref 已注册）
        this.$nextTick(() => {
          this._a11y = ZY.bindModalA11y(this.$refs.submitModal, {
            isOpen: () => this.submitOpen,
            onEscape: () => { this.submitOpen = false; },
          });
          this.$watch('submitOpen', () => this._a11y.sync());
        });
      },

      // 课程下拉按专业分组（GET /api/courses 的 items 带 major_name）
      get courseGroups() {
        const groups = {};
        for (const c of this.courses) {
          const major = c.major_name || '未分专业';
          if (!groups[major]) groups[major] = {major: major, items: []};
          groups[major].items.push(c);
        }
        return Object.values(groups);
      },

      async openSubmit(docId, fileName) {
        this.submitOpen = true; this.submitError = '';
        this.submitDocId = docId;
        this.submitTitle = fileName.replace(/\.[^.]+$/, '');
        this.submitDesc = ''; this.submitCourseId = '';
        this.submitChapterId = ''; this.submitChapters = [];
        if (!this.courses.length) {
          try {
            const data = await ZY.api.get('/api/courses?size=100');
            this.courses = data.items || [];
          } catch (e) { this.submitError = e.message; }
        }
      },

      async loadChapters() {
        this.submitChapterId = ''; this.submitChapters = [];
        if (!this.submitCourseId) return;
        try {
          const data = await ZY.api.get('/api/courses/' + this.submitCourseId + '/chapters');
          // 章节树扁平化，用全角空格缩进体现层级
          const flat = [];
          const walk = (nodes, depth) => {
            for (const n of nodes || []) {
              flat.push({id: n.id, label: '　'.repeat(depth) + n.title});
              walk(n.children, depth + 1);
            }
          };
          walk(data.items, 0);
          this.submitChapters = flat;
        } catch (e) { this.submitError = e.message; }
      },

      async submitResource() {
        if (!this.submitTitle.trim()) { this.submitError = '请填写标题'; return; }
        if (!this.submitCourseId) { this.submitError = '请选择目标课程'; return; }
        this.loading = true; this.submitError = '';
        try {
          await ZY.api.post('/api/documents/' + this.submitDocId + '/submit', {
            title: this.submitTitle.trim(),
            description: this.submitDesc.trim() || null,
            course_id: Number(this.submitCourseId),
            chapter_id: this.submitChapterId ? Number(this.submitChapterId) : null,
          });
          // 局部更新：行内改挂「审核中」徽章（消灭整页 reload）
          this.submitted[this.submitDocId] = true;
          this.submitOpen = false;
          ZY.toast('已提交审核，可在列表查看进度');
        } catch (e) { this.submitError = e.message; }
        finally { this.loading = false; }
      },

      statusLabel(s) {
        return s === 'parsed' ? '已完成' : (s === 'failed' ? '解析失败' : '解析中');
      },

      async createSemester() {
        if (!this.semesterName.trim()) { this.error = '请填写学期名称'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/library/semesters', {name: this.semesterName.trim()});
          // 同名幂等返回已有（200）：已存在则跳过插入，仅提示
          if (!document.getElementById('sem-section-' + data.id)) {
            appendSemesterSection(data);
          }
          this.semesterName = ''; this.showSemesterForm = false;
          ZY.toast('学期已创建');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },

      async createCourse() {
        if (!this.courseName.trim()) { this.error = '请填写课程名称'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/library/courses', {
            name: this.courseName.trim(),
            semester_id: this.courseSemesterId ? Number(this.courseSemesterId) : null,
            description: this.courseDesc.trim() || null,
          });
          appendCourseCard(data);
          this.courseName = ''; this.courseDesc = ''; this.showCourseForm = false;
          ZY.toast('课程已创建，可以开始上传资料');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },

      async createChapter(courseId) {
        if (!this.chapterTitle.trim()) { this.error = '请填写章节标题'; return; }
        this.loading = true; this.error = '';
        try {
          const ch = await ZY.api.post('/api/library/courses/' + courseId + '/chapters',
            {title: this.chapterTitle.trim()});
          appendChapterBadge(courseId, ch);
          this.chapterTitle = ''; this.chapterFormFor = null;
          ZY.toast('章节已添加');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },

      // multipart 上传：ZY.api 仅支持 JSON，此处保留 fetch（全站唯一例外）
      async upload(courseId) {
        if (!this.uploadFile) { this.error = '请先选择要上传的文件'; return; }
        this.loading = true; this.uploading = true; this.error = '';
        try {
          const fd = new FormData();
          fd.append('file', this.uploadFile);
          fd.append('course_id', String(courseId));
          const resp = await fetch('/api/documents', {method: 'POST', body: fd});
          const data = await resp.json().catch(() => ({}));
          if (resp.status === 401) { window.location.href = '/login'; return; }
          if (!resp.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : null;
            throw new Error(detail || ('上传失败 ' + resp.status));
          }
          const doc = {
            id: data.document_id,
            file_name: this.uploadFile.name,
            status: data.status || 'parsing',
            status_url: data.status_url || ('/api/documents/' + data.document_id + '/status'),
          };
          if (!this.pending[courseId]) this.pending[courseId] = [];
          this.pending[courseId].push(doc);
          this.uploadFile = null; this.uploadFormFor = null;
          ZY.toast('上传成功，正在解析入库');
          // 经由响应式数组取回代理对象再轮询修改——直接改原始对象会绕过 Alpine 响应式
          const docRef = this.pending[courseId][this.pending[courseId].length - 1];
          if (docRef.status === 'parsing') this.pollStatus(docRef);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; this.uploading = false; }
      },

      // 每 3 秒轮询解析状态，直到 parsed / failed
      pollStatus(doc) {
        const timer = setInterval(async () => {
          try {
            const s = await ZY.api.get(doc.status_url);
            doc.status = s.status;
            if (s.status !== 'parsing') {
              clearInterval(timer);
              ZY.toast(s.status === 'parsed' ? '「' + doc.file_name + '」解析完成' : '「' + doc.file_name + '」解析失败',
                s.status === 'parsed' ? 'success' : 'error');
            }
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };
})();
