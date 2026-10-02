/* 知屿 KnowIsle base.html 的全局脚本（CSP 外置化：原内联 <script> 迁入本文件）。
 * 职责：通知未读角标轮询结果接收（HTMX afterRequest → 角标数字）。
 * 依赖 htmx 事件，无 Alpine 依赖；defer 加载即可。
 */
(function () {
  'use strict';
  document.body.addEventListener('htmx:afterRequest', function (evt) {
    if (!evt.detail.pathInfo || evt.detail.pathInfo.requestPath !== '/api/notifications/unread-count') return;
    try {
      var data = JSON.parse(evt.detail.xhr.responseText);
      var badge = document.getElementById('notif-badge');
      if (!badge) return;
      badge.textContent = data.unread > 99 ? '99+' : data.unread;
      badge.classList.toggle('hidden', !data.unread);
    } catch (e) { /* 接口未就绪时忽略 */ }
  });
})();
