/* 共享 DOM 渲染：模板 <template> 是唯一结构来源，避免 SSR/客户端视觉漂移。 */
window.ZY = window.ZY || {};
(function () {
  'use strict';
  var ICONS = {
    'chevron-right': '<path d="M9 6l6 6-6 6"/>',
    'star-solid': '<path fill="currentColor" stroke="none" d="M12 2l2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 17.3 5.9 20.6l1.4-6.8L2.2 9.1l6.9-.8L12 2z"/>',
  };
  function iconSvg(name, cls) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', cls || 'h-4 w-4');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = ICONS[name] || '';
    return svg;
  }
  function fromTemplate(id) {
    var template = document.getElementById(id);
    if (!template) throw new Error('缺少共享模板：' + id);
    return template.content.firstElementChild.cloneNode(true);
  }
  function slot(root, name) { return root.querySelector('[data-slot="' + name + '"]'); }
  function courseCard(course) {
    var node = fromTemplate('course-card-tpl');
    var link = slot(node, 'link');
    if (link) link.href = '/courses/' + course.id;
    slot(node, 'initial').textContent = (course.name || '?').charAt(0);
    slot(node, 'name').textContent = course.name || '';
    slot(node, 'description').textContent = course.description || '暂无简介';
    return node;
  }
  function chapterNode(chapter) {
    var node = fromTemplate('chapter-node-tpl');
    node.setAttribute('data-chapter-id', chapter.id);
    node.setAttribute('x-data', '{ open: true }');
    var button = slot(node, 'title');
    button.dataset.chapterTitle = chapter.title;
    button.setAttribute('x-bind:aria-pressed', 'selectedChapter === ' + chapter.id);
    button.setAttribute('x-bind:class', 'selectedChapter === ' + chapter.id +
      " ? 'bg-brand-50 text-brand-700 font-medium ring-1 ring-brand-200' : 'text-secondary hover:bg-surface-inset'");
    button.setAttribute('x-on:click', 'selectChapter(' + chapter.id + ')');
    button.textContent = chapter.title;
    return node;
  }
  window.ZY.ui = {iconSvg: iconSvg, fromTemplate: fromTemplate, slot: slot, courseCard: courseCard, chapterNode: chapterNode};
})();
