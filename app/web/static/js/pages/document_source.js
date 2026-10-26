/* 原文溯源：来源返回、hash 定位、章节当前态与块内搜索。 */
(function () {
  'use strict';

  var HIGHLIGHT_CLASSES = ['ring-2', 'ring-accent-300', 'border-accent-300'];
  var hits = [];
  var currentHit = -1;

  function reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  function scrollTo(el) {
    el.scrollIntoView({behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center'});
  }
  function safeInternalPath(value) {
    return value && value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') ? value : '';
  }
  function initBackLink() {
    const link = document.getElementById('source-back');
    if (!link) return;
    let from = safeInternalPath(new URLSearchParams(location.search).get('from'));
    if (!from && document.referrer) {
      try {
        const ref = new URL(document.referrer);
        if (ref.origin === location.origin && ref.pathname !== location.pathname) {
          from = safeInternalPath(ref.pathname + ref.search + ref.hash);
        }
      } catch (e) { /* 非法 referrer 忽略 */ }
    }
    if (from) {
      link.href = from;
      link.querySelector('span').textContent = '返回来源页面';
    } else if (link.getAttribute('data-scope') === 'public' && link.getAttribute('data-course-id')) {
      link.href = '/courses/' + link.getAttribute('data-course-id');
      link.querySelector('span').textContent = '返回课程空间';
    }
  }
  function highlightHash() {
    document.querySelectorAll('.chunk-card.ring-2').forEach(function (el) {
      el.classList.remove.apply(el.classList, HIGHLIGHT_CLASSES);
    });
    if (!location.hash) return;
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (e) { return; }
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add.apply(el.classList, HIGHLIGHT_CLASSES);
    if (window.ZY.motion) ZY.motion.highlight(el);
    scrollTo(el);
  }
  function initTocSpy() {
    const pills = document.querySelectorAll('[data-sec-pill]');
    if (!pills.length || typeof IntersectionObserver === 'undefined') return;
    var ACTIVE = ['bg-brand-600', 'text-white', 'shadow-sm'];
    var IDLE = ['text-muted'];
    function setActive(sec) {
      pills.forEach(function (pill) {
        const on = pill.getAttribute('data-sec-pill') === sec;
        pill.classList.remove.apply(pill.classList, ACTIVE);
        pill.classList.remove.apply(pill.classList, IDLE);
        pill.classList.add.apply(pill.classList, on ? ACTIVE : IDLE);
        pill.setAttribute('aria-current', on ? 'location' : 'false');
      });
    }
    var visible = new Set();
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      });
      if (!visible.size) return;
      var top = null;
      visible.forEach(function (el) {
        if (!top || el.getBoundingClientRect().top < top.getBoundingClientRect().top) top = el;
      });
      const sec = top.getAttribute('data-sec');
      if (sec) setActive(sec);
    }, {rootMargin: '-72px 0px -60% 0px'});
    document.querySelectorAll('.chunk-card').forEach(function (el) { io.observe(el); });
  }

  function clearMarks() {
    document.querySelectorAll('mark.source-hit').forEach(function (mark) {
      mark.replaceWith(document.createTextNode(mark.textContent));
    });
    document.querySelectorAll('.chunk-content').forEach(function (el) { el.normalize(); });
    hits = [];
    currentHit = -1;
  }
  function showHit(index) {
    if (!hits.length) return;
    currentHit = (index + hits.length) % hits.length;
    hits.forEach(function (mark, i) { mark.classList.toggle('source-hit-current', i === currentHit); });
    const status = document.getElementById('source-search-status');
    status.textContent = '第 ' + (currentHit + 1) + ' / ' + hits.length + ' 处';
    scrollTo(hits[currentHit]);
    hits[currentHit].setAttribute('tabindex', '-1');
    hits[currentHit].focus({preventScroll: true});
  }
  function searchChunks(query) {
    clearMarks();
    const status = document.getElementById('source-search-status');
    const term = query.trim().toLocaleLowerCase();
    if (!term) {
      status.textContent = '';
      updateButtons();
      return;
    }
    document.querySelectorAll('.chunk-content').forEach(function (root) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(function (node) {
        const text = node.nodeValue;
        const lower = text.toLocaleLowerCase();
        let start = 0;
        let index;
        const fragment = document.createDocumentFragment();
        let found = false;
        while ((index = lower.indexOf(term, start)) >= 0) {
          found = true;
          fragment.appendChild(document.createTextNode(text.slice(start, index)));
          const mark = document.createElement('mark');
          mark.className = 'source-hit';
          mark.textContent = text.slice(index, index + term.length);
          fragment.appendChild(mark);
          hits.push(mark);
          start = index + term.length;
        }
        if (found) {
          fragment.appendChild(document.createTextNode(text.slice(start)));
          node.replaceWith(fragment);
        }
      });
    });
    status.textContent = hits.length ? '共 ' + hits.length + ' 处命中' : '未找到匹配内容';
    updateButtons();
    if (hits.length) showHit(0);
  }
  function updateButtons() {
    const prev = document.getElementById('source-search-prev');
    const next = document.getElementById('source-search-next');
    if (prev) prev.disabled = !hits.length;
    if (next) next.disabled = !hits.length;
  }
  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  window.addEventListener('hashchange', highlightHash);
  ready(function () {
    initBackLink();
    highlightHash();
    initTocSpy();
    const input = document.getElementById('source-search-input');
    if (!input) return;
    input.addEventListener('input', function () { searchChunks(input.value); });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        showHit(currentHit + (event.shiftKey ? -1 : 1));
      }
    });
    document.getElementById('source-search-prev').addEventListener('click', function () { showHit(currentHit - 1); });
    document.getElementById('source-search-next').addEventListener('click', function () { showHit(currentHit + 1); });
    document.getElementById('source-search-clear').addEventListener('click', function () {
      input.value = '';
      searchChunks('');
      input.focus();
    });
  });
})();
