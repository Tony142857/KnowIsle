/* 知屿 KnowIsle Markdown 渲染（客户端）：marked 解析 + DOMPurify 白名单消毒。
 * 依赖 vendor 的 marked（UMD 全局 marked）与 purify（全局 DOMPurify），
 * 须在 Alpine 之前加载（模板在 x-init / x-effect 中调用 ZY.md.render）。
 *
 * 契约：
 *   ZY.md.render(text)      → 安全 HTML 字符串（GFM，单换行折行）；
 *                             入参为空串 / null 返回 ''；marked 或 DOMPurify
 *                             缺失时降级为转义纯文本 + <br> 折行（不抛异常）。
 *   ZY.md.renderInto(el, t) → el.innerHTML = render(t)。
 * 消毒白名单：仅排版标签（标题/列表/代码/引用/表格/链接等），禁 img/script/
 * 事件属性；所有链接强制 target=_blank rel=noopener。
 */
window.ZY = window.ZY || {};
(function () {
  'use strict';

  var ALLOWED_TAGS = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr', 'blockquote', 'pre', 'code',
    'ul', 'ol', 'li', 'strong', 'em', 'del', 'a',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
  ];
  var ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'colspan', 'rowspan'];

  // 所有渲染产物的链接统一新窗口打开并禁 opener（站内溯源链亦适用）
  if (window.DOMPurify) {
    DOMPurify.addHook('afterSanitizeAttributes', function (node) {
      if (node.tagName === 'A') {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener');
      }
    });
  }

  function escapeHtml(s) {
    return s
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function render(text) {
    if (!text) return '';
    var src = String(text);
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
      return '<p>' + escapeHtml(src).replace(/\n/g, '<br>') + '</p>';
    }
    var html = marked.parse(src, {gfm: true, breaks: true});
    return DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ALLOWED_TAGS,
      ALLOWED_ATTR: ALLOWED_ATTR,
    });
  }

  ZY.md = {
    render: render,
    renderInto: function (el, text) { el.innerHTML = render(text); },
  };
})();
