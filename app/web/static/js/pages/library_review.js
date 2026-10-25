/* 复习工具：章节范围控制、请求竞态保护、失败恢复与结果导出。 */
(function () {
  'use strict';

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      const area = document.createElement('textarea');
      area.value = text;
      area.className = 'sr-only';
      document.body.appendChild(area);
      area.select();
      try {
        if (!document.execCommand('copy')) throw new Error('copy failed');
        resolve();
      } catch (e) { reject(e); }
      finally { area.remove(); }
    });
  }

  window.reviewPage = function (presetCourseId) {
    return {
      courseKey: '', personalCourses: [], publicCourses: [],
      coursesLoading: false, coursesError: '', publicCoursesError: '',
      chapters: [], chaptersLoading: false, chaptersError: '', selectedChapters: [],
      selectionError: '', chapterRequest: 0,
      outlineState: 'idle', outline: '', outlineError: '', outlineUsage: null, outlineRequest: 0,
      quizChapterId: '', quizCount: 5,
      quizState: 'idle', quizError: '', questions: [], quizRequest: 0,
      picked: {},

      httpErrorHint(status, detail) {
        if (detail) return detail;
        if (status === 404) return '课程不存在或无权限访问';
        if (status === 429) return '今日 AI 额度已用完，请明天再试（或在个人中心配置自定义 Key）';
        if (status === 502) return 'AI 服务暂时不可用，请稍后重试';
        return '请求失败（HTTP ' + status + '）';
      },
      async init() {
        if (this.coursesLoading) return;
        this.coursesLoading = true;
        this.coursesError = ''; this.publicCoursesError = '';
        const [libResult, pubResult] = await Promise.allSettled([
          ZY.api.get('/api/library/'), ZY.api.get('/api/courses?size=100'),
        ]);
        if (libResult.status === 'fulfilled') {
          const mine = [];
          for (const s of libResult.value.semesters || []) mine.push(...(s.courses || []));
          mine.push(...(libResult.value.uncategorized_courses || []));
          this.personalCourses = mine;
        } else this.coursesError = libResult.reason.message || '个人课程加载失败';
        if (pubResult.status === 'fulfilled') this.publicCourses = pubResult.value.items || [];
        else this.publicCoursesError = '公共课程加载失败：' + (pubResult.reason.message || '请重试');
        if (!this.coursesError && presetCourseId != null) this.preset(presetCourseId);
        this.coursesLoading = false;
      },
      preset(courseId) {
        const key = this.personalCourses.some(c => c.id === courseId)
          ? 'personal:' + courseId
          : (this.publicCourses.some(c => c.id === courseId) ? 'public:' + courseId : '');
        if (key) { this.courseKey = key; this.onCourseChange(); }
      },
      courseId() { return this.courseKey ? Number(this.courseKey.split(':')[1]) : null; },
      toggleChapter(id) {
        const i = this.selectedChapters.indexOf(id);
        if (i >= 0) {
          this.selectedChapters.splice(i, 1);
          this.selectionError = '';
        } else if (this.selectedChapters.length >= 50) {
          this.selectionError = '最多选择 50 个章节，请先取消部分选择';
        } else {
          this.selectedChapters.push(id);
          this.selectionError = '';
        }
      },
      selectAll() {
        this.selectedChapters = this.chapters.slice(0, 50).map((ch) => ch.id);
        this.selectionError = this.chapters.length > 50 ? '章节超过 50 个，已选择前 50 个' : '';
      },
      clearChapters() {
        this.selectedChapters = [];
        this.selectionError = '';
      },
      pick(i, j) { this.picked[i] = this.picked[i] === j ? null : j; },

      async onCourseChange() {
        const request = ++this.chapterRequest;
        this.outlineRequest += 1;
        this.quizRequest += 1;
        this.chapters = []; this.selectedChapters = []; this.selectionError = '';
        this.chaptersError = '';
        this.quizChapterId = ''; this.questions = []; this.quizState = 'idle';
        this.picked = {};
        this.outlineState = 'idle'; this.outline = ''; this.outlineError = '';
        if (!this.courseKey) return;
        const [scope, id] = this.courseKey.split(':');
        const url = scope === 'personal'
          ? '/api/library/courses/' + id + '/chapters'
          : '/api/courses/' + id + '/chapters';
        this.chaptersLoading = true;
        try {
          const data = await ZY.api.get(url);
          if (request !== this.chapterRequest) return;
          const flat = [];
          const walk = (nodes, depth) => {
            for (const n of nodes || []) {
              flat.push({id: n.id, label: '　'.repeat(depth) + n.title});
              walk(n.children, depth + 1);
            }
          };
          walk(data.items, 0);
          this.chapters = flat;
        } catch (e) {
          if (request === this.chapterRequest) this.chaptersError = e.message || '章节加载失败';
        } finally {
          if (request === this.chapterRequest) this.chaptersLoading = false;
        }
      },
      retryChapters() { this.onCourseChange(); },

      async generateOutline() {
        const courseId = this.courseId();
        if (!courseId || this.outlineState === 'loading') return;
        if (this.selectedChapters.length > 50) {
          this.outlineError = '最多选择 50 个章节';
          this.outlineState = 'error';
          return;
        }
        const request = ++this.outlineRequest;
        const key = this.courseKey;
        this.outlineState = 'loading'; this.outlineError = '';
        try {
          const data = await ZY.api.post('/api/review/outline', {
            course_id: courseId,
            chapter_ids: this.selectedChapters.length ? this.selectedChapters : null,
          });
          if (request !== this.outlineRequest || key !== this.courseKey) return;
          this.outline = data.outline || '';
          this.outlineUsage = {token_usage: data.token_usage, latency_ms: data.latency_ms};
          this.outlineState = this.outline ? 'done' : 'error';
          if (!this.outline) this.outlineError = '未生成有效大纲，请重试';
        } catch (e) {
          if (request !== this.outlineRequest) return;
          this.outlineError = this.httpErrorHint(e.status, e.message);
          this.outlineState = 'error';
        }
      },
      async copyOutline() {
        if (!this.outline) return;
        try {
          await copyText(this.outline);
          ZY.toast('复习大纲已复制', 'success');
        } catch (e) { ZY.toast('复制失败，请手动选择大纲', 'error'); }
      },
      printOutline() {
        if (!this.outline) return;
        document.body.classList.add('print-outline-only');
        const cleanup = () => document.body.classList.remove('print-outline-only');
        window.addEventListener('afterprint', cleanup, {once: true});
        window.print();
        setTimeout(cleanup, 1000);
      },

      async generateQuiz() {
        const courseId = this.courseId();
        if (!courseId || !this.quizChapterId || this.quizState === 'loading') return;
        const count = Math.max(1, Math.min(10, Number(this.quizCount) || 5));
        this.quizCount = count;
        const request = ++this.quizRequest;
        const key = this.courseKey;
        const chapterId = Number(this.quizChapterId);
        this.quizState = 'loading'; this.quizError = '';
        try {
          const data = await ZY.api.post('/api/review/quiz', {
            course_id: courseId, chapter_id: chapterId, count: count,
          });
          if (request !== this.quizRequest || key !== this.courseKey) return;
          this.questions = Array.isArray(data.questions) ? data.questions : [];
          this.picked = {};
          this.quizState = 'done';
        } catch (e) {
          if (request !== this.quizRequest) return;
          this.quizError = this.httpErrorHint(e.status, e.message);
          this.quizState = 'error';
        }
      },
    };
  };
})();
