(function () {
  'use strict';

  // 草稿经 ZY.drafts 写入 localStorage（键 zhiyu:post-draft:v1），按账号隔离
  const DRAFT_NAME = 'post-draft';

  window.postNew = function (initialBoard, userId) {
    return {
      board: initialBoard, title: '', content: '', tagsInput: '', bountyScore: 10,
      courseId: '', chapterId: '', courses: [], chapters: [], score: null,
      mode: 'edit', previewHtml: '', draftMessage: '', published: false,
      experienceSections: [
        {key: '背景', label: '背景', desc: '专业、排名、英语、科研/项目经历等基本情况', placeholder: '个人基本情况：专业、排名、英语、科研/项目经历等…'},
        {key: '时间线', label: '时间线', desc: '按时间顺序记录关键节点', placeholder: '按时间顺序记录关键节点，如：3 月报名 → 5 月夏令营 → 7 月 offer…'},
        {key: '经验要点', label: '经验要点', desc: '你认为最有价值的经验，分条写清楚', placeholder: '你认为最有价值的经验，分条写清楚…'},
        {key: '避坑提示', label: '避坑提示', desc: '踩过的坑、需要注意的细节', placeholder: '踩过的坑、需要注意的细节…'},
      ],
      sections: {'背景': '', '时间线': '', '经验要点': '', '避坑提示': ''},
      loading: false, error: '', coursesError: '', chaptersError: '', saveTimer: null,

      get courseGroups() {
        const groups = {};
        for (const c of this.courses) {
          const major = c.major_name || '未分专业';
          if (!groups[major]) groups[major] = {major: major, items: []};
          groups[major].items.push(c);
        }
        return Object.values(groups);
      },
      get contentLength() {
        return (this.board === 'experience' ? this.buildExperienceContent() : this.content).length;
      },
      get dirty() {
        return !!(this.title || this.content || this.tagsInput || this.courseId
          || Object.values(this.sections).some(Boolean));
      },
      async init() {
        this.restoreDraft();
        const preselect = new URLSearchParams(window.location.search).get('course_id');
        if (preselect && /^\d+$/.test(preselect)) this.courseId = preselect;
        await Promise.all([this.loadCourses(), this.loadScore()]);
        if (this.courseId) await this.loadChapters(true);
        for (const key of ['title', 'content', 'tagsInput', 'bountyScore', 'courseId', 'chapterId', 'board']) {
          this.$watch(key, () => this.scheduleDraftSave());
        }
        this.$watch('sections', () => this.scheduleDraftSave(), {deep: true});
        window.addEventListener('zy:session-expired', () => this.saveDraftNow());
        window.addEventListener('beforeunload', event => {
          if (this.dirty && !this.published) this.saveDraftNow();
          if (!this.dirty || this.published) return;
          event.preventDefault();
          event.returnValue = '';
        });
      },
      restoreDraft() {
        const draft = ZY.drafts.load(DRAFT_NAME, String(userId));
        if (!draft) return;
        for (const key of ['board', 'title', 'content', 'tagsInput', 'bountyScore', 'courseId', 'chapterId']) {
          if (draft[key] !== undefined && draft[key] !== null) this[key] = draft[key];
        }
        if (draft.sections) this.sections = {...this.sections, ...draft.sections};
        this.draftMessage = '已恢复当前账号的本地草稿';
      },
      saveDraftNow() {
        if (this.published) return;
        const saved = ZY.drafts.save(DRAFT_NAME, {
          board: this.board, title: this.title, content: this.content,
          tagsInput: this.tagsInput, bountyScore: this.bountyScore,
          courseId: this.courseId, chapterId: this.chapterId, sections: this.sections,
        }, String(userId));
        this.draftMessage = saved ? '草稿已自动保存' : '本地草稿保存失败';
      },
      scheduleDraftSave() {
        if (this.published) return;
        clearTimeout(this.saveTimer);
        this.saveTimer = setTimeout(() => this.saveDraftNow(), 350);
      },
      clearDraft() {
        clearTimeout(this.saveTimer);
        ZY.drafts.remove(DRAFT_NAME, String(userId));
        this.draftMessage = '';
      },
      switchBoard(board) {
        this.board = board;
        this.mode = 'edit';
        if (board === 'experience') { this.courseId = ''; this.chapterId = ''; }
      },
      showPreview() {
        this.mode = 'preview';
        const source = this.board === 'experience' ? this.buildExperienceContent() : this.content;
        this.previewHtml = ZY.md.render(source);
        this.$nextTick(() => ZY.md.highlight(this.$root));
      },
      leave(url) {
        if (!this.dirty) { window.location.href = url; return; }
        this.$dispatch('zy-ask', {
          title: '离开发帖页', message: '内容已保存在本地草稿，确定离开吗？',
          confirmText: '离开', onConfirm: () => { window.location.href = url; },
        });
      },
      async loadScore() {
        try {
          const me = await ZY.api.get('/api/users/me');
          this.score = me.score;
        } catch (e) { this.score = null; }
      },
      async loadCourses() {
        this.coursesError = '';
        try {
          const data = await ZY.api.get('/api/courses?size=100');
          this.courses = data.items || [];
          if (this.courseId && !this.courses.some(c => String(c.id) === String(this.courseId))) {
            this.courseId = ''; this.chapterId = '';
          }
        } catch (e) { this.coursesError = '公共课程加载失败：' + e.message; }
      },
      async loadChapters(preserve) {
        if (!preserve) this.chapterId = '';
        this.chapters = []; this.chaptersError = '';
        if (!this.courseId) return;
        try {
          const data = await ZY.api.get('/api/courses/' + this.courseId + '/chapters');
          const flat = [];
          const walk = (nodes, depth) => {
            for (const n of nodes || []) {
              flat.push({id: n.id, label: '　'.repeat(depth) + n.title});
              walk(n.children, depth + 1);
            }
          };
          walk(data.items, 0);
          this.chapters = flat;
          if (this.chapterId && !flat.some(ch => String(ch.id) === String(this.chapterId))) this.chapterId = '';
        } catch (e) { this.chaptersError = '章节加载失败：' + e.message; }
      },
      parseTags() {
        return this.tagsInput.split(/[,，]/).map(t => t.trim()).filter(Boolean).slice(0, 5);
      },
      buildExperienceContent() {
        const parts = [];
        for (const sec of this.experienceSections) {
          const body = (this.sections[sec.key] || '').trim();
          if (body) parts.push('## ' + sec.key + '\n' + body);
        }
        return parts.join('\n\n');
      },
      validate() {
        if (!this.title.trim()) return '请填写标题';
        let content = this.content.trim();
        if (this.board === 'experience') {
          if (!(this.sections['背景'] || '').trim()) return '请至少填写「背景」小节';
          content = this.buildExperienceContent();
        }
        if (!content) return '请填写正文';
        if (this.board === 'qa' && !this.courseId) return '问答帖必须选择关联课程';
        if (this.board === 'bounty') {
          const n = Number(this.bountyScore);
          if (!Number.isInteger(n) || n < 1 || n > 1000) return '悬赏贡献分须为 1~1000 的整数';
          if (this.score !== null && n > this.score) return '贡献分余额不足，无法托管该悬赏';
        }
        return '';
      },
      submit() {
        this.error = this.validate();
        if (this.error) return;
        if (this.board === 'bounty') {
          const cost = Number(this.bountyScore);
          this.$dispatch('zy-ask', {
            title: '确认托管悬赏',
            message: '发布将托管 ' + cost + ' 贡献分，采纳响应后结算给响应者。当前余额 '
              + (this.score === null ? '未知' : this.score + ' 分') + '，托管后余额 '
              + (this.score === null ? '以服务端为准' : (this.score - cost) + ' 分') + '。',
            confirmText: '托管并发布', onConfirm: () => this.doSubmit(),
          });
          return;
        }
        this.doSubmit();
      },
      async doSubmit() {
        if (this.loading) return;
        this.saveDraftNow();
        this.loading = true; this.error = '';
        ZY.setBusy(this, true);
        try {
          const body = {
            board: this.board, title: this.title.trim(),
            content: this.board === 'experience' ? this.buildExperienceContent() : this.content.trim(),
            course_id: this.courseId ? Number(this.courseId) : null,
            chapter_id: this.chapterId ? Number(this.chapterId) : null,
            tags: this.parseTags(),
          };
          if (this.board === 'bounty') body.bounty_score = Number(this.bountyScore);
          const data = await ZY.api.post('/api/posts', body);
          this.published = true;
          this.clearDraft();
          ZY.toast('发布成功', 'success');
          window.location.href = '/posts/' + data.post_id;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; ZY.setBusy(this, false); }
      },
    };
  };
})();
