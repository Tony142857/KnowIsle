/* board.html 页面交互（CSP 外置，v1.1「翡翠绿·海岛」）。
 * 加载方式：{% block head %} 内同步 <script src>（不带 defer）。
 * 注意：base.html 的 head block 位于 alpine-*.min.js（defer）之后，defer 脚本会在
 * Alpine.start() 的 queueMicrotask 之后才执行，x-data 引用的全局函数会找不到；
 * 同步脚本在解析期执行、先于 defer 的 Alpine 启动，故必须同步加载。
 *
 * v1.1：请求统一 ZY.api；列表时间统一 ZY.relTime；客户端降级路径不再自带一套
 * 漂移的卡片模板，而是克隆模板内 <template id="board-post-card">（与 SSR post_card
 * 宏逐节点对齐的单一结构来源）填充渲染，含 AI 摘要折叠卡 / 徽章 / 作者届别。 */
(function () {
  'use strict';

  function slot(root, name) {
    return root.querySelector('[data-slot="' + name + '"]');
  }

  // 克隆 <template id="board-post-card"> 渲染一张帖子卡片（结构与 SSR post_card 宏一致）
  function buildCard(p, board) {
    const tpl = document.getElementById('board-post-card');
    if (!tpl) return null;
    const node = tpl.content.firstElementChild.cloneNode(true);

    if (p.status === 'featured') node.classList.add('card-accent-left');
    const title = slot(node, 'title');
    title.href = '/posts/' + p.id;
    title.textContent = p.title;

    // 徽章（与 SSR 宏同一优先级口径：精华 > 求援结算/悬赏 > 已采纳 > AI 已答）
    if (p.status === 'featured') slot(node, 'badge-featured').hidden = false;
    if (board === 'bounty') {
      if (p.has_accepted) {
        slot(node, 'badge-settled').hidden = false;
      } else {
        const b = slot(node, 'badge-bounty');
        b.hidden = false;
        b.textContent = '悬赏 ' + p.bounty_score + ' 分';
      }
    } else if (p.has_accepted) {
      slot(node, 'badge-accepted').hidden = false;
    } else if (p.has_ai_answer && board === 'qa') {
      slot(node, 'badge-ai').hidden = false;
    }

    slot(node, 'excerpt').textContent = p.excerpt || '';

    // 经验帖 AI 摘要折叠卡（Markdown 客户端渲染）
    if (board === 'experience' && p.ai_summary) {
      slot(node, 'ai-summary').hidden = false;
      ZY.md.renderInto(slot(node, 'ai-summary-body'), p.ai_summary);
    }

    const author = p.author || {};
    if (author.avatar_url) {
      const img = slot(node, 'avatar');
      img.hidden = false;
      img.src = author.avatar_url;
    }
    slot(node, 'author').textContent = author.nickname || '';
    // 列表接口的 author 摘要不下发 grade：有值才显示届别徽章（缺省隐藏）
    if (board === 'experience' && author.grade) {
      const g = slot(node, 'grade');
      g.hidden = false;
      g.textContent = author.grade;
    }
    const time = slot(node, 'time');
    time.textContent = ZY.relTime(p.created_at);
    if (p.created_at) time.title = String(p.created_at).slice(0, 16).replace('T', ' ');
    // 元信息数字槽位（图标已在模板内渲染，这里只填数字，与 SSR 宏对齐）
    slot(node, 'views').textContent = p.view_count;
    slot(node, 'comments').textContent = p.comment_count;
    slot(node, 'votes').textContent = p.vote_score;

    const tagsHost = slot(node, 'tags');
    for (const t of p.tags || []) {
      const a = document.createElement('a');
      a.href = '/boards/' + board + '?tag=' + encodeURIComponent(t);
      a.className = 'rounded bg-slate-100 px-1.5 py-0.5 text-slate-500 transition-colors hover:bg-brand-50 hover:text-brand-700';
      a.textContent = '#' + t;
      tagsHost.appendChild(a);
    }
    return node;
  }

  // 板块列表客户端补载（SSR 降级路径）：GET /api/posts 拉取一页帖子
  window.boardList = function (board, page, tag, courseId) {
    return {
      board: board, items: [], loading: false, loaded: false, error: '',
      async load() {
        this.loading = true; this.error = '';
        try {
          let url = '/api/posts?board=' + board + '&page=' + page + '&size=20';
          if (tag) url += '&tag=' + encodeURIComponent(tag);
          if (courseId) url += '&course_id=' + courseId;
          const data = await ZY.api.get(url);
          this.items = data.items || [];
          this.loaded = true;
          this.$nextTick(() => {
            const host = document.getElementById('board-fallback-list');
            if (!host) return;
            // 首屏 stagger 入场：第 2/3/4 张依次延迟（与 SSR 循环同一口径）
            const delays = ['', 'd-80', 'd-150', 'd-300'];
            this.items.forEach((p, i) => {
              const card = buildCard(p, board);
              if (!card) return;
              if (i > 0 && i <= 3) card.classList.add(delays[i]);
              host.appendChild(card);
            });
          });
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
