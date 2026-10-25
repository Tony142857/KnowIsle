/* AI 问答：可中止的 SSE 流、失败重试、额度与阅读位置管理。 */
(function () {
  'use strict';

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
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

  window.chatPage = function () {
    var pageData = JSON.parse(document.getElementById('page-data').textContent);
    return {
      courseId: pageData.course_id,
      courseName: pageData.course_name,
      scope: pageData.default_scope || 'personal',
      question: '', sending: false, composing: false,
      messages: [], nextMessageId: 1, activeMessage: null,
      controller: null, autoScroll: true,
      quota: null, quotaError: '',

      init() {
        const draft = ZY.drafts.load('chat:' + this.courseId);
        if (draft && typeof draft.question === 'string') this.question = draft.question;
        this.$watch('question', () => this.saveDraft());
        window.addEventListener('zy:session-expired', () => this.saveDraft());
        window.addEventListener('beforeunload', () => this.saveDraft());
        this.loadQuota();
      },
      saveDraft() { ZY.drafts.save('chat:' + this.courseId, {question: this.question}); },
      async loadQuota() {
        this.quotaError = '';
        try { this.quota = await ZY.api.get('/api/users/me/quota'); }
        catch (e) { this.quotaError = e.message || '额度信息加载失败'; }
      },
      quotaText() {
        if (!this.quota) return '';
        if (this.quota.custom_key) return '已配置自定义 Key，不占官方额度';
        const free = this.quota.remaining_free || 0;
        const bonus = this.quota.bonus_balance || 0;
        return '今日剩余 ' + (free + bonus) + ' 次（免费 ' + free + ' + 兑换 ' + bonus + '）';
      },
      scopeText() {
        if (this.scope === 'personal') return '仅检索你在本课程个人知识库中的资料。';
        if (this.scope === 'public') return '仅检索本课程已上架的公共资料。';
        return '同时检索个人资料与公共上架资料，引用会标明来源。';
      },
      httpErrorHint(status) {
        if (status === 401) return '登录已过期，请重新登录后再试';
        if (status === 404) return '课程不存在或无权限访问';
        if (status === 429) return '今日 AI 额度已用完，请明天再试，或前往个人中心兑换额度 / 配置自定义 Key';
        if (status === 422) return '问题或检索范围不符合要求，请检查后重试';
        if (status === 405) return '当前问答请求方式不受支持，请刷新页面后重试';
        if (status >= 500) return 'AI 服务暂时不可用，请稍后重试';
        return '请求失败（HTTP ' + status + '）';
      },
      handleEnter(event) {
        if (event.isComposing || this.composing || event.keyCode === 229 || event.shiftKey) return;
        event.preventDefault();
        this.send();
      },
      onScroll() {
        const el = this.$refs.msgBox;
        if (!el) return;
        this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 56;
      },
      resumeScroll() {
        this.autoScroll = true;
        this.scrollBottom(true);
      },
      scrollBottom(force) {
        if (!this.autoScroll && !force) return;
        this.$nextTick(() => {
          const el = this.$refs.msgBox;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },
      async copy(text) {
        if (!text) return;
        try {
          await copyText(text);
          ZY.toast('已复制到剪贴板', 'success');
        } catch (e) { ZY.toast('复制失败，请手动选择文本', 'error'); }
      },
      newChat() {
        if (this.controller) this.controller.abort();
        this.sending = false;
        this.activeMessage = null;
        this.messages = [];
        this.question = '';
        ZY.drafts.remove('chat:' + this.courseId);
        this.autoScroll = true;
        this.$nextTick(() => this.$refs.questionInput.focus());
      },
      stop() {
        if (!this.sending) return;
        if (this.controller) this.controller.abort();
        const ai = this.activeMessage;
        if (ai) {
          ai.done = true;
          ai.stopped = true;
          if (!ai.text && !ai.error) ai.text = '已停止回答。';
        }
        this.sending = false;
        this.activeMessage = null;
        this.controller = null;
      },
      retry(index) {
        if (this.sending) return;
        const ai = this.messages[index];
        if (!ai || ai.role !== 'ai') return;
        const q = ai.question;
        this.messages.splice(index, 1);
        const user = this.messages[index - 1];
        if (user && user.role === 'user' && user.text === q) this.messages.splice(index - 1, 1);
        this.ask(q);
      },
      send() {
        const q = this.question.trim();
        if (!q || this.sending) return;
        this.question = '';
        this.ask(q);
      },

      async ask(q) {
        if (!q || this.sending) return;
        ZY.drafts.save('chat:' + this.courseId, {question: q});
        this.sending = true;
        this.autoScroll = true;
        const controller = new AbortController();
        this.controller = controller;
        this.messages.push({id: this.nextMessageId++, role: 'user', text: q});
        this.messages.push({
          id: this.nextMessageId++, role: 'ai', question: q, text: '', citations: [],
          usage: null, error: '', done: false, stopped: false,
        });
        const ai = this.messages[this.messages.length - 1];
        this.activeMessage = ai;
        this.scrollBottom();
        let terminal = false;
        try {
          const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question: q, course_id: this.courseId, scope: this.scope}),
            signal: controller.signal,
          });
          if (resp.status === 401) {
            this.question = q;
            this.saveDraft();
            window.dispatchEvent(new CustomEvent('zy:session-expired'));
            const next = location.pathname + location.search + location.hash;
            location.href = '/login?next=' + encodeURIComponent(next);
          }
          if (!resp.ok || !resp.body) {
            const data = await resp.json().catch(() => ({}));
            ai.error = typeof data.detail === 'string' ? data.detail : this.httpErrorHint(resp.status);
            ai.done = true;
            terminal = true;
          } else {
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            const handleFrame = (frame) => {
              for (const line of frame.split('\n')) {
                if (!line.startsWith('data:')) continue;
                let evt;
                try { evt = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
                if (evt.type === 'token' && !terminal) ai.text += evt.text || '';
                else if (evt.type === 'citations' && !terminal) ai.citations = evt.items || [];
                else if (evt.type === 'done') {
                  ai.usage = evt;
                  ai.done = true;
                  terminal = true;
                } else if (evt.type === 'error') {
                  ai.error = evt.message || '生成出错，请重试';
                  ai.done = true;
                  terminal = true;
                }
                this.scrollBottom();
                if (terminal) break;
              }
            };
            while (!terminal) {
              const {done, value} = await reader.read();
              if (done) break;
              buf += decoder.decode(value, {stream: true}).replace(/\r\n/g, '\n');
              let idx;
              while (!terminal && (idx = buf.indexOf('\n\n')) >= 0) {
                const frame = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                handleFrame(frame);
              }
            }
            if (!terminal && buf.trim()) handleFrame(buf);
            if (!ai.done) {
              ai.done = true;
              ai.error = '连接在回答完成前中断，请重试';
            } else if (!ai.text && !ai.error && !ai.stopped) {
              ai.error = '未收到回答内容，请重试';
            }
          }
        } catch (e) {
          if (e.name !== 'AbortError') {
            ai.error = '网络错误：' + e.message;
            ai.done = true;
          }
        } finally {
          const wasStopped = ai.stopped;
          const isCurrent = this.activeMessage === ai;
          ai.done = true;
          if (isCurrent) {
            this.sending = false;
            this.activeMessage = null;
            this.controller = null;
            if (ai.error && !wasStopped) {
              this.question = q;
              ZY.toast('回答失败，问题已保留在输入框', 'error');
            }
            if (!ai.error && !wasStopped) this.loadQuota();
          }
          this.scrollBottom();
        }
      },
    };
  };
})();
