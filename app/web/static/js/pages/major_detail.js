/* 专业详情页（major_detail.html）：课程空间创建/申请（v1.1 外置化，原模板内联 x-data 迁入）。
 * courseApply —— POST /api/courses：管理员直接开通（active），学生提交申请（pending）。
 * 成功后 ZY.toast 反馈 + 局部更新（admin 直建客户端追加课程卡片），消灭整页 reload。
 * 加载时序：本文件在 head 块内同步加载（勿加 defer），须在 defer 的 Alpine 启动前定义工厂函数。 */
(function () {
  'use strict';

  function appendCourseCard(course) {
    const grid = document.getElementById('course-grid');
    if (!grid) return;
    const empty = document.getElementById('course-empty');
    if (empty) empty.classList.add('hidden');
    const card = ZY.ui.courseCard(course);
    grid.appendChild(card);
    if (window.ZY.motion) ZY.motion.enter(card);
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
