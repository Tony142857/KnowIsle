/* 复习工具页（library_review.html）交互逻辑（CSP 外置化：原内联 <script> 迁入本文件，逻辑不变）。
 * 预选课程 ID 经 x-data 属性参数注入（CSP 不限制属性）——本文件在 head 同步加载。 */
(function () {
  'use strict';

  window.reviewPage = function (presetCourseId) {
    return {
      // 课程与章节选择
      courseKey: '', personalCourses: [], publicCourses: [],
      coursesLoading: false, coursesError: '',
      chapters: [], chaptersLoading: false, selectedChapters: [],
      // 大纲三态：idle / loading / done / error
      outlineState: 'idle', outline: '', outlineError: '', outlineUsage: null,
      // 习题三态
      quizChapterId: '', quizCount: 5,
      quizState: 'idle', quizError: '', questions: [],
      // 习题选项选中态：题目序号 -> 选项序号（仅本地标记，不影响判分）
      picked: {},

      httpErrorHint(status, detail) {
        if (detail) return detail;
        if (status === 401) return '登录已过期，请重新登录后再试';
        if (status === 404) return '课程不存在或无权限访问';
        if (status === 429) return '今日 AI 额度已用完，请明天再试（或在个人中心配置自定义 Key）';
        if (status === 502) return 'AI 服务暂时不可用，请稍后重试';
        return '请求失败（HTTP ' + status + '）';
      },

      async init() {
        this.coursesLoading = true;
        try {
          // 个人课程（含章节树）+ 公共课程空间（模块 A4：复习工具对双库均可用）
          const [libResp, pubResp] = await Promise.all([
            fetch('/api/library/'),
            fetch('/api/courses?size=100'),
          ]);
          const lib = await libResp.json().catch(() => ({}));
          if (!libResp.ok) throw new Error(lib.detail || ('加载个人课程失败 ' + libResp.status));
          const mine = [];
          for (const s of lib.semesters || []) mine.push(...(s.courses || []));
          mine.push(...(lib.uncategorized_courses || []));
          this.personalCourses = mine;
          if (pubResp.ok) {
            const pub = await pubResp.json().catch(() => ({}));
            this.publicCourses = pub.items || [];
          }
          if (presetCourseId != null) this.preset(presetCourseId);
        } catch (e) {
          this.coursesError = e.message;
        } finally {
          this.coursesLoading = false;
        }
      },

      preset(courseId) {
        const key = this.personalCourses.some(c => c.id === courseId)
          ? 'personal:' + courseId
          : (this.publicCourses.some(c => c.id === courseId) ? 'public:' + courseId : '');
        if (key) { this.courseKey = key; this.onCourseChange(); }
      },

      courseId() {
        return this.courseKey ? Number(this.courseKey.split(':')[1]) : null;
      },

      toggleChapter(id) {
        const i = this.selectedChapters.indexOf(id);
        if (i >= 0) this.selectedChapters.splice(i, 1);
        else this.selectedChapters.push(id);
      },

      // 单选式标记：重复点击同一选项取消选中
      pick(i, j) {
        this.picked[i] = this.picked[i] === j ? null : j;
      },

      async onCourseChange() {
        this.chapters = []; this.selectedChapters = [];
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
          const resp = await fetch(url);
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载章节失败 ' + resp.status));
          // 章节树扁平化，用全角空格缩进体现层级（与 library.html 同一约定）
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
          this.coursesError = e.message;
        } finally {
          this.chaptersLoading = false;
        }
      },

      async post(url, body) {
        const resp = await fetch(url, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(this.httpErrorHint(resp.status, data.detail));
        return data;
      },

      async generateOutline() {
        const courseId = this.courseId();
        if (!courseId || this.outlineState === 'loading') return;
        this.outlineState = 'loading'; this.outlineError = ''; this.outline = '';
        try {
          const data = await this.post('/api/review/outline', {
            course_id: courseId,
            chapter_ids: this.selectedChapters.length ? this.selectedChapters : null,
          });
          this.outline = data.outline || '';
          this.outlineUsage = {token_usage: data.token_usage, latency_ms: data.latency_ms};
          this.outlineState = 'done';
        } catch (e) {
          this.outlineError = e.message;
          this.outlineState = 'error';
        }
      },

      async generateQuiz() {
        const courseId = this.courseId();
        if (!courseId || !this.quizChapterId || this.quizState === 'loading') return;
        this.quizState = 'loading'; this.quizError = ''; this.questions = [];
        this.picked = {};
        try {
          const data = await this.post('/api/review/quiz', {
            course_id: courseId,
            chapter_id: Number(this.quizChapterId),
            count: this.quizCount,
          });
          this.questions = data.questions || [];
          this.quizState = 'done';
        } catch (e) {
          this.quizError = e.message;
          this.quizState = 'error';
        }
      },
    };
  };
})();
