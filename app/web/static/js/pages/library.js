/* 个人知识库：局部课程管理、真实进度上传、解析恢复与投稿状态。 */
(function () {
  'use strict';

  var MAX_FILE_SIZE = 50 * 1024 * 1024;
  var POLL_TIMEOUT = 10 * 60 * 1000;
  var ACCEPTED = ['.pdf', '.pptx', '.docx', '.md', '.markdown'];

  function semesterGroupsEl() { return document.getElementById('semester-groups'); }
  function hideLibraryEmpty() {
    const el = document.getElementById('library-empty');
    if (el) el.classList.add('hidden');
  }
  function reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  function extension(name) {
    const i = name.lastIndexOf('.');
    return i >= 0 ? name.slice(i).toLowerCase() : '';
  }

  function appendSemesterSection(s) {
    hideLibraryEmpty();
    const host = semesterGroupsEl();
    if (host) {
      const section = document.createElement('section');
      section.className = 'mb-8 animate-fade-up';
      section.id = 'sem-section-' + s.id;
      const h2 = document.createElement('h2');
      h2.className = 'mb-3 flex items-center gap-2 text-lg font-semibold text-primary';
      h2.textContent = s.name;
      const count = document.createElement('span');
      count.className = 'text-xs font-normal text-muted';
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

  function appendCourseCard(c) {
    hideLibraryEmpty();
    const grid = document.getElementById(c.semester_id ? 'course-grid-' + c.semester_id : 'course-grid-none');
    if (!grid) return;
    const uncat = document.getElementById('sem-section-none');
    if (!c.semester_id && uncat) uncat.classList.remove('hidden');
    const card = ZY.ui.fromTemplate('library-course-tpl');
    card.querySelector('[data-role="name"]').textContent = c.name;
    card.querySelector('[data-slot="chat"]').href = '/library/chat?course_id=' + c.id;
    card.querySelector('[data-slot="review"]').href = '/library/review?course_id=' + c.id;
    card.querySelector('[data-slot="chapter-toggle"]').setAttribute('x-on:click',
      'chapterFormFor = chapterFormFor === ' + c.id + ' ? null : ' + c.id + '; chapterTitle = \'\'');
    card.querySelector('[data-slot="upload-toggle"]').setAttribute('data-upload-course', c.id);
    card.querySelector('[data-slot="upload-toggle"]').setAttribute('x-on:click', 'openUpload(' + c.id + ')');
    card.querySelector('[data-slot="chapter-list"]').id = 'chapter-list-' + c.id;
    const chapterForm = card.querySelector('[data-slot="chapter-form"]');
    chapterForm.setAttribute('x-show', 'chapterFormFor === ' + c.id);
    const title = card.querySelector('[data-slot="chapter-title"]');
    title.id = 'new-chapter-' + c.id;
    title.previousElementSibling.htmlFor = title.id;
    card.querySelector('[data-slot="chapter-add"]').setAttribute('x-on:click', 'createChapter(' + c.id + ')');
    const uploadForm = card.querySelector('[data-slot="upload-form"]');
    uploadForm.setAttribute('x-show', 'uploadFormFor === ' + c.id);
    uploadForm.setAttribute('x-bind:class', "{'is-dragging': draggingCourse === " + c.id + '}');
    uploadForm.setAttribute('x-on:dragenter.prevent', 'draggingCourse = ' + c.id);
    uploadForm.setAttribute('x-on:drop.prevent', 'selectDropped(' + c.id + ', $event)');
    const input = card.querySelector('[data-slot="file-input"]');
    input.id = 'upload-file-' + c.id;
    input.previousElementSibling.htmlFor = input.id;
    input.setAttribute('x-on:change', 'selectFile(' + c.id + ', $event.target.files[0] || null)');
    card.querySelector('[data-slot="upload-button"]').setAttribute('x-on:click', 'upload(' + c.id + ')');
    card.querySelector('[data-slot="upload-button"]').setAttribute('x-bind:disabled',
      'uploading || !uploadFile || uploadCourseId !== ' + c.id);
    const pendingTemplate = card.querySelector('[data-slot="pending-list"]');
    pendingTemplate.setAttribute('x-for', 'd in (pending[' + c.id + '] || [])');
    pendingTemplate.content.querySelector('[data-slot="reupload"]').setAttribute(
      'x-on:click', 'prepareReupload(' + c.id + ', d.id)');
    grid.appendChild(card);
    if (window.ZY.motion) ZY.motion.enter(card);
  }

  function appendChapterBadge(courseId, ch) {
    const list = document.getElementById('chapter-list-' + courseId);
    if (!list) return;
    const badge = document.createElement('span');
    badge.className = 'badge badge-slate animate-fade-up';
    badge.textContent = ch.title;
    badge.appendChild(ZY.ui.iconSvg('chevron-right', 'w-3 h-3 text-muted'));
    list.appendChild(badge);
  }

  function xhrUpload(file, courseId, onProgress) {
    return new Promise(function (resolve, reject) {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/documents');
      xhr.responseType = 'json';
      xhr.upload.addEventListener('progress', function (event) {
        if (event.lengthComputable) onProgress(Math.min(100, Math.round(event.loaded / event.total * 100)));
      });
      xhr.addEventListener('load', function () {
        const data = xhr.response || {};
        if (xhr.status === 401) {
          const next = location.pathname + location.search + location.hash;
          location.href = '/login?next=' + encodeURIComponent(next);
          reject(new Error('登录已过期，请重新登录'));
          return;
        }
        if (xhr.status < 200 || xhr.status >= 300) {
          const err = new Error(typeof data.detail === 'string' ? data.detail : '上传失败（HTTP ' + xhr.status + '）');
          err.status = xhr.status;
          reject(err);
          return;
        }
        resolve(data);
      });
      xhr.addEventListener('error', function () { reject(new Error('网络错误，文件仍保留，请重试')); });
      xhr.addEventListener('abort', function () { reject(new Error('上传已取消，可重新提交')); });
      const fd = new FormData();
      fd.append('file', file);
      fd.append('course_id', String(courseId));
      xhr.send(fd);
    });
  }

  window.libraryPage = function () {
    return {
      loading: false, error: '',
      showSemesterForm: false, showCourseForm: false,
      semesterName: '', courseName: '', courseSemesterId: '', courseDesc: '',
      chapterFormFor: null, chapterTitle: '',
      uploadFormFor: null, uploadCourseId: null, uploadFile: null,
      uploading: false, uploadProgress: 0, draggingCourse: null, retryDocId: null,
      submitOpen: false, submitDocId: null, submitTitle: '', submitDesc: '',
      submitCourseId: '', submitChapterId: '', submitChapters: [],
      submitError: '', courses: [], submitted: {}, retryingSubmission: null,
      pending: {}, docStates: {}, pollers: {},

      init() {
        document.querySelectorAll('[data-rel-time]').forEach(function (el) {
          const text = ZY.relTime(el.getAttribute('data-rel-time'));
          if (text) el.textContent = text;
        });
        document.querySelectorAll('[data-document-id]').forEach((el) => {
          const id = Number(el.getAttribute('data-document-id'));
          const doc = {
            id: id,
            course_id: Number(el.getAttribute('data-course-id')),
            file_name: el.getAttribute('data-file-name'),
            status: el.getAttribute('data-status'),
            status_url: '/api/documents/' + id + '/status',
            parse_error: '', poll_error: '',
          };
          this.docStates[id] = doc;
          if (doc.status === 'parsing') this.pollDocument(doc);
        });
        this.$nextTick(() => {
          this._a11y = ZY.bindModalA11y(this.$refs.submitModal, {
            isOpen: () => this.submitOpen,
            onEscape: () => { if (!this.loading) this.submitOpen = false; },
          });
          this.$watch('submitOpen', () => this._a11y.sync());
        });
        if (new URLSearchParams(location.search).get('upload') === '1') {
          const first = document.querySelector('[data-upload-course]');
          if (first) this.openUpload(Number(first.getAttribute('data-upload-course')));
        }
      },

      get courseGroups() {
        const groups = {};
        for (const c of this.courses) {
          const major = c.major_name || '未分专业';
          if (!groups[major]) groups[major] = {major: major, items: []};
          groups[major].items.push(c);
        }
        return Object.values(groups);
      },
      documentStatus(id, fallback) { return this.docStates[id] ? this.docStates[id].status : fallback; },
      documentError(id, fallback) {
        const doc = this.docStates[id];
        if (!doc) return fallback;
        return doc.parse_error || fallback || (doc.status === 'failed' ? '解析失败，请选择同文件重传' : '');
      },
      statusLabel(s) { return s === 'parsed' ? '已完成' : (s === 'failed' ? '解析失败' : '解析中'); },
      statusClass(s) { return s === 'parsed' ? 'badge-brand' : (s === 'failed' ? 'bg-danger-soft text-danger' : 'badge-slate'); },
      canSubmit(id, initiallyAllowed) {
        return initiallyAllowed && this.documentStatus(id, '') === 'parsed' && !this.submitted[id];
      },

      openUpload(courseId) {
        this.uploadFormFor = this.uploadFormFor === courseId ? null : courseId;
        this.uploadCourseId = courseId;
        this.uploadFile = null;
        this.retryDocId = null;
        this.uploadProgress = 0;
        this.error = '';
      },
      prepareReupload(courseId, docId) {
        this.uploadFormFor = courseId;
        this.uploadCourseId = courseId;
        this.retryDocId = docId;
        this.uploadFile = null;
        this.error = '请选择与失败文档相同的文件；系统会去重并重新解析。';
        this.$nextTick(() => {
          const input = document.getElementById('upload-file-' + courseId);
          if (input) input.focus();
        });
      },
      selectFile(courseId, file) {
        this.uploadCourseId = courseId;
        this.uploadFile = file;
        this.error = '';
      },
      selectDropped(courseId, event) {
        this.draggingCourse = null;
        this.uploadFormFor = courseId;
        this.selectFile(courseId, event.dataTransfer.files[0] || null);
      },
      validateFile(file) {
        if (!file) return '请先选择要上传的文件';
        if (file.size > MAX_FILE_SIZE) return '文件超过 50MB 限制，请压缩或拆分后重试';
        const ext = extension(file.name);
        if (ext === '.doc' || ext === '.ppt') return '旧版 ' + ext + ' 暂不支持，请另存为 .docx/.pptx 后上传';
        if (!ACCEPTED.includes(ext)) return '仅支持 PDF、PPTX、DOCX、Markdown 文件';
        return '';
      },

      async upload(courseId) {
        const validation = this.validateFile(this.uploadFile);
        if (validation) { this.error = validation; return; }
        if (this.uploading) return;
        const file = this.uploadFile;
        this.uploading = true;
        this.uploadProgress = 0;
        this.error = '';
        try {
          const data = await xhrUpload(file, courseId, (value) => { this.uploadProgress = value; });
          this.uploadProgress = 100;
          const doc = {
            id: data.document_id,
            course_id: courseId,
            file_name: file.name,
            status: data.status || 'parsing',
            status_url: data.status_url || ('/api/documents/' + data.document_id + '/status'),
            parse_error: '', poll_error: '',
          };
          const existingRow = document.getElementById('document-' + doc.id);
          if (data.dedup && existingRow) {
            if (this.docStates[doc.id]) Object.assign(this.docStates[doc.id], doc);
            else this.docStates[doc.id] = doc;
            this.removePending(doc.id);
            existingRow.scrollIntoView({behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center'});
            existingRow.classList.add('ring-2', 'ring-brand-300');
            setTimeout(() => existingRow.classList.remove('ring-2', 'ring-brand-300'), 1800);
            ZY.toast(data.retry ? '已定位原文档并重新解析' : '文件已存在，已定位到原文档', 'info');
          } else {
            if (!this.pending[courseId]) this.pending[courseId] = [];
            if (!this.pending[courseId].some((d) => d.id === doc.id)) this.pending[courseId].push(doc);
            this.docStates[doc.id] = this.pending[courseId].find((d) => d.id === doc.id);
            ZY.toast(data.dedup ? '文件已存在，已恢复原解析任务' : '上传成功，正在解析入库', 'success');
          }
          const input = document.getElementById('upload-file-' + courseId);
          if (input) input.value = '';
          this.uploadFile = null;
          this.retryDocId = null;
          this.uploadFormFor = null;
          if (doc.status === 'parsing') this.pollDocument(this.docStates[doc.id]);
        } catch (e) {
          this.error = e.message || '上传失败，请重试';
        } finally {
          this.uploading = false;
        }
      },

      removePending(id) {
        Object.keys(this.pending).forEach((courseId) => {
          this.pending[courseId] = this.pending[courseId].filter((d) => d.id !== id);
        });
      },
      resumePolling(id) {
        const doc = this.docStates[id];
        if (!doc) return;
        doc.poll_error = '';
        this.pollDocument(doc, true);
      },
      pollDocument(doc, restart) {
        if (!doc || (this.pollers[doc.id] && !restart)) return;
        if (this.pollers[doc.id]) clearTimeout(this.pollers[doc.id]);
        const state = {started: Date.now(), failures: 0};
        const tick = async () => {
          if (Date.now() - state.started >= POLL_TIMEOUT) {
            doc.poll_error = '解析状态检查超时，任务可能仍在运行，请点击“继续检查”。';
            delete this.pollers[doc.id];
            return;
          }
          try {
            const data = await ZY.api.get(doc.status_url);
            state.failures = 0;
            doc.status = data.status;
            doc.parse_error = data.parse_error || '';
            doc.poll_error = '';
            if (data.status !== 'parsing') {
              delete this.pollers[doc.id];
              ZY.toast('「' + doc.file_name + '」' + (data.status === 'parsed' ? '解析完成，可溯源和投稿' : '解析失败'),
                data.status === 'parsed' ? 'success' : 'error');
              return;
            }
          } catch (e) {
            state.failures += 1;
            doc.poll_error = '暂时无法获取解析状态，将自动重试（第 ' + state.failures + ' 次）。';
          }
          const delay = state.failures ? Math.min(3000 * Math.pow(2, state.failures - 1), 15000) : 3000;
          this.pollers[doc.id] = setTimeout(tick, delay);
        };
        this.pollers[doc.id] = setTimeout(tick, 0);
      },

      async retrySubmission(docId) {
        if (this.retryingSubmission) return;
        this.retryingSubmission = docId;
        try {
          const data = await ZY.api.post('/api/documents/' + docId + '/submit/retry');
          this.submitted[docId] = {label: data.review_status === 'pending' ? '预检中' : '审核中'};
          ZY.toast(data.queued === false ? '投稿已进入后续阶段，无需重试' : '预检任务已重新提交', 'success');
        } catch (e) { ZY.toast(e.message, 'error'); }
        finally { this.retryingSubmission = null; }
      },
      async retryPreview(docId) {
        try {
          const data = await ZY.api.post('/api/documents/' + docId + '/preview/retry');
          ZY.toast(data.preview_pending ? '预览转换已重新提交' : '预览已可用，无需重试', 'success');
        } catch (e) { ZY.toast(e.message, 'error'); }
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
        ZY.setBusy(this, true);
        try {
          const data = await ZY.api.post('/api/documents/' + this.submitDocId + '/submit', {
            title: this.submitTitle.trim(),
            description: this.submitDesc.trim() || null,
            course_id: Number(this.submitCourseId),
            chapter_id: this.submitChapterId ? Number(this.submitChapterId) : null,
          });
          this.submitted[this.submitDocId] = {label: '预检中'};
          this.submitOpen = false;
          ZY.toast(data.precheck_queued === false ? '投稿已创建，但预检待重试，请在文档行点击“重试预检”' : '已提交投稿，进入预检阶段',
            data.precheck_queued === false ? 'error' : 'success');
        } catch (e) { this.submitError = e.message; }
        finally { this.loading = false; ZY.setBusy(this, false); }
      },

      async createSemester() {
        if (!this.semesterName.trim()) { this.error = '请填写学期名称'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/library/semesters', {name: this.semesterName.trim()});
          if (!document.getElementById('sem-section-' + data.id)) appendSemesterSection(data);
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
          const ch = await ZY.api.post('/api/library/courses/' + courseId + '/chapters', {title: this.chapterTitle.trim()});
          appendChapterBadge(courseId, ch);
          this.chapterTitle = ''; this.chapterFormFor = null;
          ZY.toast('章节已添加');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
