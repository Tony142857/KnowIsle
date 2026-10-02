/* 知屿 KnowIsle 跨页复用 Alpine 组件，经 alpine:init 在 Alpine 启动前注册。
 * 依赖 api.js（window.ZY.api），须在 alpine-*.min.js 之前加载（defer 按序执行）。
 *
 * 组件清单与用法（模板内 x-data 直接调用）：
 *
 * 1) voteBox(targetType, targetId, score, myVote, loggedIn) —— 点赞/点踩开关
 *      <div x-data="voteBox('post', {{ post.id }}, {{ vote_score }}, {{ my_vote }},
 *                          {{ 'true' if user else 'false' }})">
 *        <button @click="vote(1)" :disabled="loading" ...>▲ <span x-text="score"></span></button>
 *        <button @click="vote(-1)" :disabled="loading" ...>▼</button>
 *        <span x-show="error" x-text="error" x-cloak></span>
 *      </div>
 *    语义：POST /api/votes，同值取消、异值改值；未登录点击跳 /login。
 *
 * 2) favBox(targetType, targetId, favorited, loggedIn[, countElId]) —— 收藏开关
 *      <span x-data="favBox('resource', {{ resource.id }},
 *                           {{ 'true' if is_favorited else 'false' }},
 *                           {{ 'true' if user else 'false' }}, 'fav-count')">
 *        <button @click="toggle()" :disabled="loading" ...>
 *          <span x-text="favorited ? '★ 已收藏' : '☆ 收藏'"></span>
 *        </button>
 *        <span x-show="error" x-text="error" x-cloak></span>
 *      </span>
 *    语义：POST/DELETE /api/favorites；countElId 可选，传了则同步增减该元素计数。
 *
 * 3) confirmModal([defaults]) —— 确认对话框（替代原生 confirm/prompt 的通用底座）
 *      <div x-data="confirmModal()">
 *        <button @click="ask({message: '确认停用该课程？', onConfirm: () => doDisable()})">停用</button>
 *        <div x-show="open" x-cloak class="fixed inset-0 z-50 ...">
 *          <h2 x-text="title"></h2>
 *          <p x-text="message"></p>
 *          <input x-show="withInput" x-model="input" :placeholder="placeholder">
 *          <button @click="close()">取消</button>
 *          <button @click="confirm()" :disabled="loading" x-text="confirmText"></button>
 *        </div>
 *      </div>
 *    ask(options) 选项：title / message / confirmText / withInput / placeholder /
 *    requireInput（非空才可确认）/ onConfirm(input)（异步函数，resolve 后自动关闭）。
 */
document.addEventListener('alpine:init', function () {
  'use strict';

  // 点赞 / 点踩开关（提取自 post_detail.html 投票逻辑，行为等价）
  Alpine.data('voteBox', function (targetType, targetId, score, myVote, loggedIn) {
    return {
      score: score, myVote: myVote, loading: false, error: '',
      async vote(value) {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/votes', {
            target_type: targetType, target_id: targetId, value: value,
          });
          this.score = data.score; this.myVote = data.my_vote;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  });

  // 收藏开关（合并 post_detail.html 的 postFav 与 resource_detail.html 的 favBox）
  Alpine.data('favBox', function (targetType, targetId, favorited, loggedIn, countElId) {
    return {
      favorited: favorited, loading: false, error: '',
      async toggle() {
        if (!loggedIn) { window.location.href = '/login'; return; }
        this.loading = true; this.error = '';
        try {
          await ZY.api.request(this.favorited ? 'DELETE' : 'POST', '/api/favorites', {
            target_type: targetType, target_id: targetId,
          });
          this.favorited = !this.favorited;
          if (countElId) {
            const el = document.getElementById(countElId);
            if (el) el.textContent = Math.max(0, parseInt(el.textContent, 10) + (this.favorited ? 1 : -1));
          }
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  });

  // 确认对话框底座：供后续替换原生 confirm/prompt（admin 停用课程、举报处理备注等）
  Alpine.data('confirmModal', function (defaults) {
    defaults = defaults || {};
    return {
      open: false, loading: false,
      title: defaults.title || '确认操作',
      message: defaults.message || '',
      confirmText: defaults.confirmText || '确认',
      withInput: false, placeholder: '', requireInput: false,
      input: '', _onConfirm: null,

      ask(options) {
        options = options || {};
        this.title = options.title || this.title;
        this.message = options.message || '';
        this.confirmText = options.confirmText || '确认';
        this.withInput = !!options.withInput;
        this.placeholder = options.placeholder || '';
        this.requireInput = !!options.requireInput;
        this.input = '';
        this._onConfirm = options.onConfirm || null;
        this.open = true;
      },
      close() {
        if (this.loading) return;
        this.open = false;
        this._onConfirm = null;
      },
      async confirm() {
        if (this.requireInput && !this.input.trim()) return;
        this.loading = true;
        try {
          if (this._onConfirm) await this._onConfirm(this.input.trim());
          this.open = false;
          this._onConfirm = null;
        } finally { this.loading = false; }
      },
    };
  });
});
