/* 原文溯源页（document_source.html，v1.1）：URL hash 定位高亮 + 章节导航当前高亮。
 * - 引用卡片跳转 /documents/{id}/source#{chunk_id}，命中块加 accent 高亮环并平滑滚动到位；
 * - 顶部章节目录（按 section 连续分组）点击经 hash 复用同一滚动高亮逻辑；
 * - IntersectionObserver 跟踪视口内文本块，同步高亮目录中对应的章节 pill。
 * 本文件在 head 同步加载（先于 Alpine 启动），执行时 body 尚未解析，须待 DOM 就绪再定位。 */
(function () {
  'use strict';

  var HIGHLIGHT_CLASSES = ['ring-2', 'ring-accent-300', 'border-accent-300'];

  function highlightHash() {
    document.querySelectorAll('.chunk-card.ring-2').forEach(function (el) {
      el.classList.remove.apply(el.classList, HIGHLIGHT_CLASSES);
    });
    if (!location.hash) return;
    var el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!el) return;
    el.classList.add.apply(el.classList, HIGHLIGHT_CLASSES);
    el.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  // 目录 pill 当前态：视口中最靠上的可见文本块所属 section
  function initTocSpy() {
    var pills = document.querySelectorAll('[data-sec-pill]');
    if (!pills.length || typeof IntersectionObserver === 'undefined') return;
    var ACTIVE = ['bg-brand-600', 'text-white', 'shadow-sm'];
    var IDLE = ['text-slate-500'];

    function setActive(sec) {
      pills.forEach(function (pill) {
        var on = pill.getAttribute('data-sec-pill') === sec;
        pill.classList.remove.apply(pill.classList, ACTIVE);
        pill.classList.remove.apply(pill.classList, IDLE);
        pill.classList.add.apply(pill.classList, on ? ACTIVE : IDLE);
      });
    }

    var visible = new Set();
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      });
      if (!visible.size) return;
      // 取文档序最靠前者（getBoundingClientRect 最小 top）
      var top = null;
      visible.forEach(function (el) {
        if (!top || el.getBoundingClientRect().top < top.getBoundingClientRect().top) top = el;
      });
      var sec = top.getAttribute('data-sec');
      if (sec) setActive(sec);
    }, {rootMargin: '-72px 0px -60% 0px'});
    document.querySelectorAll('.chunk-card').forEach(function (el) { io.observe(el); });
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }
  window.addEventListener('hashchange', highlightHash);
  ready(function () {
    highlightHash();
    initTocSpy();
  });
})();
