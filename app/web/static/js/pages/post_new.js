/* post_new.html 页面交互（CSP 外置，v1.0）。
 * 加载方式：{% block head %} 内同步 <script src>（原因见 board.js 头注）。 */
(function () {
  'use strict';

  window.postNew = function (initialBoard) {
    return {
      board: initialBoard,
      title: '', content: '', tagsInput: '', bountyScore: 10,
      courseId: '', chapterId: '', courses: [], chapters: [],
      // 经验长廊结构化模板四栏（与 app/community/experience.py 的 EXPERIENCE_TEMPLATE_SECTIONS 一致）
      experienceSections: [
        {key: '背景', label: '背景', desc: '专业、排名、英语、科研/项目经历等基本情况',
         placeholder: '个人基本情况：专业、排名、英语、科研/项目经历等…'},
        {key: '时间线', label: '时间线', desc: '按时间顺序记录关键节点',
         placeholder: '按时间顺序记录关键节点，如：3 月报名 → 5 月夏令营 → 7 月 offer…'},
        {key: '经验要点', label: '经验要点', desc: '你认为最有价值的经验，分条写清楚',
         placeholder: '你认为最有价值的经验，分条写清楚…'},
        {key: '避坑提示', label: '避坑提示', desc: '踩过的坑、需要注意的细节',
         placeholder: '踩过的坑、需要注意的细节…'},
      ],
      sections: {'背景': '', '时间线': '', '经验要点': '', '避坑提示': ''},
      loading: false, error: '',

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

      switchBoard(b) {
        // 切换板块时清空课程绑定（experience 板块不使用课程）
        this.board = b;
        if (b === 'experience') { this.courseId = ''; this.chapterId = ''; }
      },

      async loadCourses() {
        try {
          const resp = await fetch('/api/courses?size=100');
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载课程失败 ' + resp.status));
          this.courses = data.items || [];
        } catch (e) { this.error = e.message; }
      },

      async loadChapters() {
        this.chapterId = ''; this.chapters = [];
        if (!this.courseId) return;
        try {
          const resp = await fetch('/api/courses/' + this.courseId + '/chapters');
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
          this.chapters = flat;
        } catch (e) { this.error = e.message; }
      },

      parseTags() {
        return this.tagsInput.split(/[,，]/)
          .map(t => t.trim()).filter(t => t).slice(0, 5);
      },

      // 经验帖正文：非空小节按固定顺序组装为「## 小节 + 内容」Markdown
      buildExperienceContent() {
        const parts = [];
        for (const sec of this.experienceSections) {
          const body = (this.sections[sec.key] || '').trim();
          if (body) parts.push('## ' + sec.key + '\n' + body);
        }
        return parts.join('\n\n');
      },

      async submit() {
        this.error = '';
        if (!this.title.trim()) { this.error = '请填写标题'; return; }
        let content = this.content.trim();
        if (this.board === 'experience') {
          if (!(this.sections['背景'] || '').trim()) { this.error = '请至少填写「背景」小节'; return; }
          content = this.buildExperienceContent();
          if (!content) { this.error = '请填写正文'; return; }
        } else if (!content) { this.error = '请填写正文'; return; }
        if (this.board === 'qa' && !this.courseId) { this.error = '问答帖必须选择关联课程'; return; }
        if (this.board === 'bounty') {
          const n = Number(this.bountyScore);
          if (!Number.isInteger(n) || n < 1 || n > 1000) { this.error = '悬赏贡献分须为 1~1000 的整数'; return; }
        }
        this.loading = true;
        try {
          const body = {
            board: this.board,
            title: this.title.trim(),
            content: content,
            course_id: this.courseId ? Number(this.courseId) : null,
            chapter_id: this.chapterId ? Number(this.chapterId) : null,
            tags: this.parseTags(),
          };
          if (this.board === 'bounty') body.bounty_score = Number(this.bountyScore);
          const resp = await fetch('/api/posts', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('发布失败 ' + resp.status));
          window.location.href = '/posts/' + data.post_id;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
