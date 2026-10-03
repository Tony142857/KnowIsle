/* 原文溯源页（document_source.html）：URL hash 定位高亮（CSP 外置化：原内联 <script> 迁入本文件）。
 * 引用卡片跳转 /documents/{id}/source#{chunk_id}，命中块加 amber 高亮环并平滑滚动到位。 */
(function () {
  'use strict';

  function highlightHash() {
    document.querySelectorAll('.chunk-card.ring-2').forEach(function (el) {
      el.classList.remove('ring-2', 'ring-amber-300', 'border-amber-300');
    });
    if (!location.hash) return;
    var el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!el) return;
    el.classList.add('ring-2', 'ring-amber-300', 'border-amber-300');
    el.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  window.addEventListener('hashchange', highlightHash);
  // 本文件在 head 同步加载（先于 Alpine 启动），执行时 body 尚未解析，须待 DOM 就绪再定位
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', highlightHash);
  } else {
    highlightHash();
  }
})();
