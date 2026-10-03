/* board.html 页面交互（CSP 外置，v1.0）。
 * 加载方式：{% block head %} 内同步 <script src>（不带 defer）。
 * 注意：base.html 的 head block 位于 alpine-*.min.js（defer）之后，defer 脚本会在
 * Alpine.start() 的 queueMicrotask 之后才执行，x-data 引用的全局函数会找不到；
 * 同步脚本在解析期执行、先于 defer 的 Alpine 启动，故必须同步加载。 */
(function () {
  'use strict';

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
          const resp = await fetch(url);
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(data.detail || ('加载失败 ' + resp.status));
          this.items = data.items || [];
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  };
})();
