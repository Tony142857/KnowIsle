/* 专业详情页（major_detail.html）：课程空间创建/申请（v1.1 外置化，原模板内联 x-data 迁入）。
 * courseApply —— POST /api/courses：管理员直接开通（active），学生提交申请（pending）。
 * 成功后 ZY.toast 反馈 + 局部更新（admin 直建客户端追加课程卡片），消灭整页 reload。
 * 加载时序：本文件在 head 块内同步加载（勿加 defer），须在 defer 的 Alpine 启动前定义工厂函数。 */
(function () {
  'use strict';

  // 与 _icons.html 的 chevron-right 一致（客户端追加卡片复用同一 SVG 常量）
  const CHEVRON_SVG = '<svg class="h-3.5 w-3.5 transition-transform duration-island ease-island group-hover:translate-x-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';

  // admin 直建成功：客户端追加课程卡片到 #course-grid（结构与 SSR 卡片一致）
  function appendCourseCard(course) {
    const grid = document.getElementById('course-grid');
    if (!grid) return;
    const empty = document.getElementById('course-empty');
    if (empty) empty.classList.add('hidden');
    const a = document.createElement('a');
    a.href = '/courses/' + course.id;
    a.className = 'card card-hover group block animate-fade-up p-5';

    const head = document.createElement('div');
    head.className = 'flex items-center gap-3';
    const badge = document.createElement('span');
    badge.className = 'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-50 font-semibold text-brand-600 transition-colors duration-island ease-island group-hover:bg-brand-100';
    badge.textContent = (course.name || '?').charAt(0);
    const h2 = document.createElement('h2');
    h2.className = 'text-card-title text-slate-800 transition-colors duration-island ease-island group-hover:text-brand-700';
    h2.textContent = course.name;
    head.appendChild(badge);
    head.appendChild(h2);

    const p = document.createElement('p');
    p.className = 'mt-2 line-clamp-2 text-sm text-slate-500';
    p.textContent = course.description || '暂无简介';

    const foot = document.createElement('div');
    foot.className = 'mt-3 flex items-center justify-end';
    const hint = document.createElement('span');
    hint.className = 'flex items-center gap-1 text-xs text-slate-400 transition-colors duration-island ease-island group-hover:text-brand-600';
    hint.textContent = '进入课程空间 ';
    hint.insertAdjacentHTML('beforeend', CHEVRON_SVG);
    foot.appendChild(hint);

    a.appendChild(head);
    a.appendChild(p);
    a.appendChild(foot);
    grid.appendChild(a);
  }

  window.courseApply = function (majorId, isAdmin) {
    return {
      name: '', description: '', loading: false, error: '',
      applied: false, // 学生申请成功（pending 课程不进列表，仅本地状态提示）

      async submit() {
        if (!this.name.trim()) { this.error = '请填写课程名称'; return; }
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.post('/api/courses', {
            name: this.name.trim(),
            description: this.description.trim() || null,
            major_id: majorId,
          });
          if (data.applied) {
            this.applied = true;
            ZY.toast('已提交开通申请，等待管理员审批');
          } else {
            appendCourseCard(data);
            ZY.toast('课程空间已创建');
          }
          this.name = ''; this.description = '';
        } catch (e) {
          this.error = e.message;
        } finally {
          this.loading = false;
        }
      },
    };
  };
})();
