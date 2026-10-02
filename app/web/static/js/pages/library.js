/* 个人知识库页（library.html）交互逻辑（CSP 外置化：原内联 <script> 迁入本文件，逻辑不变）。
 * 模板 head 块内同步加载（勿加 defer）：须在 defer 的 Alpine 启动前定义全局工厂函数，
 * x-data="libraryPage()" 才能解析到（实测 defer 晚于 alpine 的 queueMicrotask 启动）。 */
(function () {
  'use strict';

  window.libraryPage = function () {
    return {
      loading: false, error: '',
      showSemesterForm: false, showCourseForm: false,
      semesterName: '', courseName: '', courseSemesterId: '', courseDesc: '',
      chapterFormFor: null, chapterTitle: '',
      uploadFormFor: null, uploadChapterId: '', uploadFile: null,
      // 投稿 modal 状态
      submitOpen: false, submitDocId: null, submitTitle: '', submitDesc: '',
      submitCourseId: '', submitChapterId: '', submitChapters: [],
      submitError: '', submitDone: false, courses: [],
      // 上传后待解析的文档：courseId -> [{id, file_name, status, status_url}]
      pending: {},

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
        this.submitOpen = true; this.submitDone = false; this.submitError = '';
        this.submitDocId = docId;
        this.submitTitle = fileName.replace(/\.[^.]+$/, '');
        this.submitDesc = ''; this.submitCourseId = '';
        this.submitChapterId = ''; this.submitChapters = [];
        if (!this.courses.length) {
          try {
            const resp = await fetch('/api/courses?size=100');
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || ('加载课程失败 ' + resp.status));
            this.courses = data.items || [];
          } catch (e) { this.submitError = e.message; }
        }
      },

      async loadChapters() {
        this.submitChapterId = ''; this.submitChapters = [];
        if (!this.submitCourseId) return;
        try {
          const resp = await fetch('/api/courses/' + this.submitCourseId + '/chapters');
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载章节失败 ' + resp.status));
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
        try {
          await this.post('/api/documents/' + this.submitDocId + '/submit', {
            title: this.submitTitle.trim(),
            description: this.submitDesc.trim() || null,
            course_id: Number(this.submitCourseId),
            chapter_id: this.submitChapterId ? Number(this.submitChapterId) : null,
          });
          this.submitDone = true;
          setTimeout(() => window.location.reload(), 1200);
        } catch (e) { this.submitError = e.message; }
      },

      statusLabel(s) {
        return s === 'parsed' ? '已完成' : (s === 'failed' ? '解析失败' : '解析中');
      },

      async post(url, body) {
        this.loading = true; this.error = '';
        try {
          const resp = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('请求失败 ' + resp.status));
          return data;
        } finally { this.loading = false; }
      },

      async createSemester() {
        if (!this.semesterName.trim()) { this.error = '请填写学期名称'; return; }
        try {
          await this.post('/api/library/semesters', {name: this.semesterName.trim()});
          window.location.reload();
        } catch (e) { this.error = e.message; }
      },

      async createCourse() {
        if (!this.courseName.trim()) { this.error = '请填写课程名称'; return; }
        try {
          await this.post('/api/library/courses', {
            name: this.courseName.trim(),
            semester_id: this.courseSemesterId ? Number(this.courseSemesterId) : null,
            description: this.courseDesc.trim() || null,
          });
          window.location.reload();
        } catch (e) { this.error = e.message; }
      },

      async createChapter(courseId) {
        if (!this.chapterTitle.trim()) { this.error = '请填写章节标题'; return; }
        try {
          await this.post('/api/library/courses/' + courseId + '/chapters',
            {title: this.chapterTitle.trim()});
          window.location.reload();
        } catch (e) { this.error = e.message; }
      },

      async upload(courseId) {
        if (!this.uploadFile) { this.error = '请先选择要上传的文件'; return; }
        this.loading = true; this.error = '';
        try {
          const fd = new FormData();
          fd.append('file', this.uploadFile);
          fd.append('course_id', String(courseId));
          if (this.uploadChapterId) fd.append('chapter_id', this.uploadChapterId);
          const resp = await fetch('/api/documents', {method: 'POST', body: fd});
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('上传失败 ' + resp.status));
          const doc = {
            id: data.document_id,
            file_name: this.uploadFile.name,
            status: data.status || 'parsing',
            status_url: data.status_url || ('/api/documents/' + data.document_id + '/status'),
          };
          if (!this.pending[courseId]) this.pending[courseId] = [];
          this.pending[courseId].push(doc);
          this.uploadFile = null; this.uploadChapterId = ''; this.uploadFormFor = null;
          // 经由响应式数组取回代理对象再轮询修改——直接改原始对象会绕过 Alpine 响应式
          const docRef = this.pending[courseId][this.pending[courseId].length - 1];
          if (docRef.status === 'parsing') this.pollStatus(docRef);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },

      // 每 3 秒轮询解析状态，直到 parsed / failed
      pollStatus(doc) {
        const timer = setInterval(async () => {
          try {
            const resp = await fetch(doc.status_url);
            if (!resp.ok) return;
            const s = await resp.json();
            doc.status = s.status;
            if (s.status !== 'parsing') clearInterval(timer);
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };
})();
