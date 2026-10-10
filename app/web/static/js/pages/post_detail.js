/* post_detail.html 页面交互（CSP 外置，v1.1）。
 * voteBox / favBox 为跨页共享组件，经 alpine:init 注册于 /static/js/components.js。
 * 加载方式：{% block head %} 内同步 <script src>（原因见 board.js 头注）。
 * 局部更新代替整页刷新：评论/回复插入 <template id="comment-node-tpl"> 克隆节点（与 SSR 宏同构），
 * 采纳广播 zy:accepted 事件局部标记，AI 首答/摘要轮询到终态后局部替换卡片。 */
(function () {
  'use strict';

  // 页面数据（模板内 <script type="application/json" id="post-page-data">，CSP 允许的惰性数据块）
  function pageData() {
    const el = document.getElementById('post-page-data');
    if (!el) return {post_id: null, board: '', is_author: false, user: null};
    try { return JSON.parse(el.textContent); }
    catch (e) { return {post_id: null, board: '', is_author: false, user: null}; }
  }

  // 新评论节点：克隆模板 + 填充槽位（正文 Markdown 由调用方 renderInto）
  function buildCommentNode(commentId, canAccept) {
    const pd = pageData();
    const tpl = document.getElementById('comment-node-tpl');
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.id = 'comment-' + commentId;
    node.setAttribute('x-data', 'commentBox(' + commentId + ', 0, 0, true, ' + (canAccept ? 'true' : 'false') + ')');
    const u = pd.user || {nickname: '我', avatar_url: null};
    const avatar = node.querySelector('[data-slot="avatar"]');
    if (u.avatar_url) {
      const img = document.createElement('img');
      img.src = u.avatar_url; img.alt = '';
      img.className = 'w-5 h-5 rounded-full object-cover';
      avatar.replaceWith(img);
    } else {
      avatar.className = 'w-5 h-5 rounded-full bg-brand-100 text-brand-700 flex items-center justify-center text-[10px] font-medium';
      avatar.textContent = (u.nickname || '我').slice(0, 1);
    }
    node.querySelector('[data-slot="nickname"]').textContent = u.nickname || '我';
    if (pd.is_author) node.querySelector('[data-slot="op-badge"]').hidden = false;
    node.querySelector('[data-slot="time"]').textContent = ZY.relTime(new Date().toISOString());
    return node;
  }

  // 新评论可被采纳的条件与 SSR 宏一致：当前用户是帖主且板块为 qa/bounty
  function newCommentCanAccept() {
    const pd = pageData();
    return pd.is_author && (pd.board === 'qa' || pd.board === 'bounty');
  }

  function bumpCommentCount() {
    const cnt = document.getElementById('comment-count');
    if (cnt) cnt.textContent = String((parseInt(cnt.textContent, 10) || 0) + 1);
  }

  // 节点所处深度（祖先 .comment-node 数）；与 SSR 宏一致：depth>=4 的子层容器不再缩进
  function nodeDepth(root) {
    let d = 0, p = root.parentElement;
    while (p) {
      if (p.classList && p.classList.contains('comment-node')) d++;
      p = p.parentElement;
    }
    return d;
  }

  // 顶部发表框
  window.commentComposer = function () {
    return {
      content: '', loading: false, error: '',
      async submit() {
        const content = this.content.trim();
        if (!content) { this.error = '评论内容不能为空'; return; }
        this.loading = true; this.error = '';
        try {
          const pd = pageData();
          const data = await ZY.api.post('/api/posts/' + pd.post_id + '/comments', {content: content});
          const node = buildCommentNode(data.comment_id, newCommentCanAccept());
          let list = document.getElementById('comment-list');
          if (!list) {
            // 原为「暂无评论」空态：换成列表容器
            list = document.createElement('div');
            list.id = 'comment-list';
            list.className = 'mt-4 space-y-3';
            const empty = document.getElementById('comment-empty');
            if (empty) empty.replaceWith(list);
            else this.$root.parentElement.appendChild(list);
          }
          list.appendChild(node);
          ZY.md.renderInto(node.querySelector('[data-slot="body"]'), content);
          this.content = '';
          bumpCommentCount();
          ZY.toast('评论发表成功', 'success');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };

  // 单条评论：点赞 + 楼内回复 + 采纳
  window.commentBox = function (commentId, score, myVote, loggedIn, canAccept) {
    return {
      score: score, myVote: myVote, loading: false, error: '',
      canAccept: canAccept,
      replyOpen: false, replyContent: '', replyError: '',
      async vote(value) {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/votes', {target_type: 'comment', target_id: commentId, value: value});
          this.score = data.score; this.myVote = data.my_vote;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      async reply() {
        const content = this.replyContent.trim();
        if (!content) { this.replyError = '回复内容不能为空'; return; }
        this.loading = true; this.replyError = '';
        try {
          const pd = pageData();
          const data = await ZY.api.post('/api/posts/' + pd.post_id + '/comments',
            {content: content, parent_id: commentId});
          const node = buildCommentNode(data.comment_id, newCommentCanAccept());
          let wrap = this.$root.querySelector(':scope > .comment-children');
          if (!wrap) {
            wrap = document.createElement('div');
            wrap.className = 'comment-children mt-3 space-y-3' +
              (nodeDepth(this.$root) < 4 ? ' ml-4 border-l-2 border-slate-200 pl-4' : '');
            this.$root.appendChild(wrap);
          }
          wrap.appendChild(node);
          ZY.md.renderInto(node.querySelector('[data-slot="body"]'), content);
          this.replyContent = ''; this.replyOpen = false;
          bumpCommentCount();
          ZY.toast('回复成功', 'success');
        } catch (e) { this.replyError = e.message; }
        finally { this.loading = false; }
      },
      async accept() {
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/posts/comments/' + commentId + '/accept', {});
          // 局部标记本卡：emerald 边框 + 「✓ 已采纳」徽章；广播事件让其余评论隐藏采纳按钮、帖头亮徽章
          this.$root.classList.add('border-emerald-300', 'ring-1', 'ring-emerald-200');
          const badge = this.$root.querySelector(':scope > .comment-head [data-slot="accepted-badge"]');
          if (badge) badge.hidden = false;
          window.dispatchEvent(new CustomEvent('zy:accepted'));
          ZY.toast(data.score_granted ? ('已采纳，对方获得 ' + data.score_granted + ' 贡献分') : '已采纳', 'success');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };

  // ---- AI 首答 / 摘要：轮询到终态后局部替换卡片 ----

  function aiCardHeader(title, palette) {
    // palette: 'brand'（问答首答）| 'accent'（经验摘要）
    const head = document.createElement('div');
    head.className = 'flex items-center gap-2';
    const badge = document.createElement('span');
    badge.className = 'rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-white ' +
      (palette === 'accent' ? 'bg-accent-500' : 'bg-brand-600');
    badge.textContent = 'AI';
    const h = document.createElement('h2');
    h.className = 'text-sm font-semibold ' + (palette === 'accent' ? 'text-accent-800' : 'text-brand-800');
    h.textContent = title;
    const note = document.createElement('span');
    note.className = 'rounded px-2 py-0.5 text-xs ' +
      (palette === 'accent' ? 'bg-accent-100 text-accent-700' : 'bg-amber-100 text-amber-700');
    note.textContent = 'AI 生成，仅供参考';
    head.append(badge, h, note);
    return head;
  }

  function renderCitationsGrid(root, citations) {
    const wrap = document.createElement('div');
    wrap.className = 'mt-4 border-t border-brand-100 pt-3';
    const label = document.createElement('p');
    label.className = 'text-xs font-medium text-slate-500';
    label.textContent = '引用来源';
    const grid = document.createElement('div');
    grid.className = 'mt-2 grid gap-2 sm:grid-cols-2';
    for (const c of citations) {
      const a = document.createElement('a');
      a.href = '/documents/' + c.document_id + '/source#' + c.chunk_id;
      a.className = 'block rounded-lg border border-slate-200 bg-white p-3 text-xs card-hover';
      const id = document.createElement('span');
      id.className = 'font-mono text-slate-400';
      id.textContent = c.chunk_id;
      const loc = document.createElement('p');
      loc.className = 'mt-1 text-slate-600';
      loc.textContent = (c.chapter || '') + (c.section ? ' · ' + c.section : '') +
        (c.page_num !== null && c.page_num !== undefined ? ' · 第 ' + c.page_num + ' 页' : '');
      const up = document.createElement('p');
      up.className = 'mt-0.5 text-slate-400';
      up.textContent = '资料由 ' + (c.uploader || '') + ' 上传';
      a.append(id, loc, up);
      grid.appendChild(a);
    }
    wrap.append(label, grid);
    root.appendChild(wrap);
  }

  // AI 首答终态：done 渲染正文 + 引用网格（[chunk_id] 转为溯源链接，与 SSR ai_segments 同构）；
  // failed 换失败文案卡
  function renderAiResult(root, data) {
    if (data.ai_answer_status !== 'done') {
      root.className = 'mt-6 rounded-card border border-slate-200 bg-white p-6 shadow-card';
      root.textContent = '';
      const h = document.createElement('h2');
      h.className = 'text-sm font-semibold text-slate-700';
      h.textContent = 'AI 助教首答';
      const p = document.createElement('p');
      p.className = 'mt-3 text-sm text-slate-500';
      p.textContent = 'AI 首答生成失败，等待同学回答。你也可以稍后在「AI 问答」中检索本课程资料。';
      root.append(h, p);
      return;
    }
    root.className = 'mt-6 rounded-card border border-brand-200 bg-brand-50/50 p-6 shadow-card';
    root.textContent = '';
    root.appendChild(aiCardHeader('AI 助教首答', 'brand'));
    let md = data.ai_first_answer || '';
    for (const c of data.ai_citations || []) {
      md = md.split('[' + c.chunk_id + ']')
        .join('[\\[' + c.chunk_id + '\\]](/documents/' + c.document_id + '/source#' + c.chunk_id + ')');
    }
    const body = document.createElement('div');
    body.className = 'md-body mt-3 text-sm text-slate-700';
    root.appendChild(body);
    ZY.md.renderInto(body, md);
    if (data.ai_citations && data.ai_citations.length) renderCitationsGrid(root, data.ai_citations);
  }

  // 经验帖摘要终态：done 渲染摘要 / failed 换失败文案卡
  function renderSummaryResult(root, data) {
    if (data.ai_summary_status !== 'done' || !data.ai_summary) {
      root.className = 'mt-6 rounded-card border border-slate-200 bg-white p-6 shadow-card';
      root.textContent = '';
      const h = document.createElement('h2');
      h.className = 'text-sm font-semibold text-slate-700';
      h.textContent = 'AI 摘要';
      const p = document.createElement('p');
      p.className = 'mt-3 text-sm text-slate-500';
      p.textContent = 'AI 摘要生成失败，可直接阅读正文。';
      root.append(h, p);
      return;
    }
    root.className = 'mt-6 rounded-card border border-accent-200 bg-accent-50/50 p-6 shadow-card';
    root.textContent = '';
    root.appendChild(aiCardHeader('AI 摘要', 'accent'));
    const body = document.createElement('div');
    body.className = 'md-body mt-3 text-sm text-slate-700';
    root.appendChild(body);
    ZY.md.renderInto(body, data.ai_summary);
  }

  // AI 首答 pending：每 3s 轮询帖子状态，终态后停轮询并局部替换卡片
  window.aiPoll = function (postId) {
    return {
      timer: null,
      start() {
        this.timer = setInterval(async () => {
          try {
            const data = await ZY.api.get('/api/posts/' + postId);
            if (data.ai_answer_status === 'pending') return;
            clearInterval(this.timer);
            renderAiResult(this.$root, data);
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };

  // 经验帖 AI 摘要 pending：每 3s 轮询，终态后局部替换卡片（v0.7）
  window.summaryPoll = function (postId) {
    return {
      timer: null,
      start() {
        this.timer = setInterval(async () => {
          try {
            const data = await ZY.api.get('/api/posts/' + postId);
            if (data.ai_summary_status === 'pending') return;
            clearInterval(this.timer);
            renderSummaryResult(this.$root, data);
          } catch (e) { /* 网络抖动时下轮重试 */ }
        }, 3000);
      },
    };
  };

  // 精华标记开关（v0.7）：POST /api/admin/posts/{id}/feature（管理员/共建者）
  window.featureBox = function (postId, featured) {
    return {
      featured: featured, loading: false, error: '',
      async toggle() {
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/admin/posts/' + postId + '/feature', {featured: !this.featured});
          this.featured = data.featured;
          ZY.toast(this.featured ? '已设为精华' : '已取消精华', 'success');
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
