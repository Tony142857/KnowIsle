/* AI 问答页（library_chat.html）交互逻辑（CSP 外置化：原内联 <script> 迁入本文件，逻辑不变）。
 * 页面参数（课程 ID / 默认检索范围）经模板惰性数据块 #page-data（application/json，不执行）注入，
 * 在组件工厂被 Alpine 调用时（DOM 已解析完）惰性读取——本文件在 head 同步加载，此时 body 尚未解析。 */
(function () {
  'use strict';

  window.chatPage = function () {
    var pageData = JSON.parse(document.getElementById('page-data').textContent);
    return {
      courseId: pageData.course_id, scope: pageData.default_scope || 'personal',
      question: '', sending: false,
      messages: [],

      httpErrorHint(status) {
        if (status === 401) return '登录已过期，请重新登录后再试';
        if (status === 404) return '课程不存在或无权限访问';
        if (status === 429) return '今日 AI 额度已用完，请明天再试（或在个人中心配置自定义 Key）';
        return '请求失败（HTTP ' + status + '）';
      },

      scrollBottom() {
        this.$nextTick(() => {
          const el = this.$refs.msgBox;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },

      async send() {
        const q = this.question.trim();
        if (!q || this.sending) return;
        this.sending = true;
        this.question = '';
        this.messages.push({role: 'user', text: q});
        this.messages.push({role: 'ai', text: '', citations: [], usage: null, error: '', done: false});
        // 必须经由响应式数组取回代理对象再修改——直接改原始对象会绕过 Alpine 响应式，模板不更新
        const ai = this.messages[this.messages.length - 1];
        this.scrollBottom();
        try {
          const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question: q, course_id: this.courseId, scope: this.scope}),
          });
          if (!resp.ok || !resp.body) {
            const data = await resp.json().catch(() => ({}));
            ai.error = data.detail || this.httpErrorHint(resp.status);
            return;
          }
          // SSE 流解析：sse-starlette 按规范以 CRLF 分行，先归一化再按 \n\n 分帧，取 data: 行的 JSON
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = '';
          while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buf += decoder.decode(value, {stream: true});
            buf = buf.replace(/\r\n/g, '\n');
            let idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
              const frame = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              for (const line of frame.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                let evt;
                try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
                if (evt.type === 'token') ai.text += evt.text;
                else if (evt.type === 'citations') ai.citations = evt.items || [];
                else if (evt.type === 'done') { ai.usage = evt; ai.done = true; }
                else if (evt.type === 'error') { ai.error = evt.message || '生成出错，请重试'; ai.done = true; }
                this.scrollBottom();
              }
            }
          }
          ai.done = true;
          if (!ai.text && !ai.error) ai.error = '未收到回答内容，请重试';
        } catch (e) {
          ai.error = '网络错误：' + e.message;
        } finally {
          this.sending = false;
          this.scrollBottom();
        }
      },
    };
  };
})();
