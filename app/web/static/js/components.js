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
 *        <div x-show="open" x-cloak role="dialog" aria-modal="true" class="fixed inset-0 z-50 ...">
 *          <h2 x-text="title"></h2>
 *          <p x-text="message"></p>
 *          <input x-show="withInput" x-model="input" :placeholder="placeholder">
 *          <button @click="close()">取消</button>
 *          <button @click="confirm()" :disabled="loading" x-text="confirmText"></button>
 *        </div>
 *      </div>
 *    ask(options) 选项：title / message / confirmText / withInput / placeholder /
 *    requireInput（非空才可确认）/ onConfirm(input)（异步函数，resolve 后自动关闭）。
 *    a11y（v1.1 F4）：组件 init 自动接入 ZY.bindModalA11y —— 打开时焦点进入弹窗、
 *    Tab 焦点圈定、Esc 关闭、关闭后焦点还原触发按钮；弹窗容器需带 role="dialog"。
 *
 * 4) ZY.toast(message, type) —— 全局轻提示（替代部分 alert/角落报错）
 *      ZY.toast('已收藏') / ZY.toast('操作失败', 'error')；type: success|error|info。
 *      容器在 base.html（#zy-toasts），本文件在 alpine:init 之外直接注册 ZY.toast。
 *
 * 5) ZY.bindModalA11y(el, {isOpen, onEscape}) —— 通用弹窗可访问性辅助（v1.1 F4）
 *      供未使用 confirmModal 的页面弹窗复用同一套行为：
 *        init() {
 *          this._a11y = ZY.bindModalA11y(this.$root,
 *            {isOpen: () => this.open, onEscape: () => this.close()});
 *          this.$watch('open', () => this._a11y.sync());
 *        }
 *      行为：打开时焦点移入首个可聚焦元素、Tab/Shift+Tab 在弹窗内圈定、
 *      Esc 触发 onEscape、关闭后焦点还原到之前的元素。
 *
 * 6) ZY.relTime(iso) —— 相对时间格式化（v1.1，列表页统一时间口径）
 *      刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前；超过 7 天落回 YYYY-MM-DD。
 */
window.ZY = window.ZY || {};
(function () {
  'use strict';
  var TOAST_STYLE = {
    success: 'border-brand-200 bg-brand-50 text-brand-700',
    error: 'border-red-200 bg-red-50 text-red-700',
    info: 'border-slate-200 bg-white text-slate-700',
  };
  var TOAST_ICON = {
    success: 'M20 6 9 17l-5-5',
    error: 'M18 6 6 18M6 6l12 12',
    info: 'M12 8h.01M12 12v4',
  };
  window.ZY.toast = function (message, type) {
    type = TOAST_STYLE[type] ? type : 'info';
    var host = document.getElementById('zy-toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'pointer-events-auto flex items-center gap-2 rounded-lg border px-4 py-2.5 text-sm shadow-lift ' +
      'animate-fade-up transition-opacity duration-300 ' + TOAST_STYLE[type];
    el.setAttribute('role', 'status');
    var icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'h-4 w-4 shrink-0');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '2');
    icon.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('d', TOAST_ICON[type]);
    icon.appendChild(path);
    var text = document.createElement('span');
    text.textContent = message;
    el.appendChild(icon);
    el.appendChild(text);
    host.appendChild(el);
    setTimeout(function () { el.classList.add('opacity-0'); }, 2600);
    setTimeout(function () { el.remove(); }, 3000);
  };

  /* 相对时间（列表页统一口径）：服务端时间戳为带时区 ISO，直接可解析 */
  window.ZY.relTime = function (iso) {
    if (!iso) return '';
    var t = new Date(iso);
    if (isNaN(t.getTime())) return '';
    var diff = Date.now() - t.getTime();
    if (diff < 0) diff = 0;
    var min = Math.floor(diff / 60000);
    if (min < 1) return '刚刚';
    if (min < 60) return min + ' 分钟前';
    var h = Math.floor(min / 60);
    if (h < 24) return h + ' 小时前';
    var d = Math.floor(h / 24);
    if (d === 1) return '昨天';
    if (d < 7) return d + ' 天前';
    return String(iso).slice(0, 10);
  };

  /* 弹窗可访问性辅助：焦点圈定 + Esc + 焦点还原。
   * 返回 {sync}：在 isOpen() 状态翻转后调用 sync() 完成进出焦点的迁移。 */
  window.ZY.bindModalA11y = function (el, opts) {
    opts = opts || {};
    var isOpen = opts.isOpen || function () { return true; };
    var onEscape = opts.onEscape || null;
    var prevFocus = null;
    var open = false;
    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), ' +
      'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function focusables() {
      return Array.prototype.filter.call(el.querySelectorAll(FOCUSABLE), function (node) {
        return node.offsetParent !== null || node === document.activeElement;
      });
    }
    function onKeydown(e) {
      if (e.key === 'Escape') {
        if (onEscape) { e.preventDefault(); onEscape(); }
        return;
      }
      if (e.key !== 'Tab') return;
      var list = focusables();
      if (!list.length) return;
      var first = list[0];
      var last = list[list.length - 1];
      if (e.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
    return {
      sync: function () {
        var nowOpen = !!isOpen();
        if (nowOpen === open) return;
        open = nowOpen;
        if (open) {
          prevFocus = document.activeElement;
          el.addEventListener('keydown', onKeydown);
          // 等 x-show 完成渲染后再聚焦（input 优先，其次首个可聚焦元素）
          setTimeout(function () {
            var input = el.querySelector('input:not([disabled])');
            var list = focusables();
            var target = (input && input.offsetParent !== null) ? input : list[0];
            if (target) target.focus();
          }, 0);
        } else {
          el.removeEventListener('keydown', onKeydown);
          if (prevFocus && prevFocus.focus) prevFocus.focus();
          prevFocus = null;
        }
      },
    };
  };
})();

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
  // v1.1 F4：init 自动接入焦点圈定 / Esc 关闭 / 焦点还原（ZY.bindModalA11y）
  Alpine.data('confirmModal', function (defaults) {
    defaults = defaults || {};
    return {
      open: false, loading: false,
      title: defaults.title || '确认操作',
      message: defaults.message || '',
      confirmText: defaults.confirmText || '确认',
      withInput: false, placeholder: '', requireInput: false,
      input: '', _onConfirm: null, _a11y: null,

      init() {
        var self = this;
        this._a11y = ZY.bindModalA11y(this.$root, {
          isOpen: function () { return self.open; },
          onEscape: function () { self.close(); },
        });
        this.$watch('open', function () { self._a11y.sync(); });
      },

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
