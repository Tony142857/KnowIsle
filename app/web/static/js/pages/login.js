/* 登录页交互（v1.1：统一走 ZY.api、发码 60s 冷却倒计时、按钮加载态）。
 * 流程（§3.1 邮箱降级通道）：
 *   1) 学号 + 校园邮箱发送验证码 → 2) 验证码校验登录 → 3) 首次登录补全资料建档。
 *
 * 加载时序说明：base.html 的 {% block head %} 渲染位置在 defer 的 alpine-*.min.js
 * 之后，defer 脚本执行前 Alpine 已 queueMicrotask 启动，此时再注册组件会来不及。
 * 因此本文件以「非 defer」方式随 head 解析同步执行（早于一切 defer 脚本），
 * 经 alpine:init 注册组件即可保证 x-data 求值前就绪。
 */
document.addEventListener('alpine:init', function () {
  'use strict';

  var RESEND_COOLDOWN = 60; // 与服务端 EMAIL_CODE_COOLDOWN_SECONDS 默认一致

  Alpine.data('loginForm', function () {
    return {
      step: 1, loading: false, error: '', hint: '',
      cooldown: 0, _timer: null,
      student_no: '', email: '', code: '',
      real_name: '', nickname: '', grade: '', major_id: '', majors: [],

      _startCooldown() {
        var self = this;
        this.cooldown = RESEND_COOLDOWN;
        if (this._timer) clearInterval(this._timer);
        this._timer = setInterval(function () {
          self.cooldown -= 1;
          if (self.cooldown <= 0) {
            clearInterval(self._timer);
            self._timer = null;
            self.cooldown = 0;
          }
        }, 1000);
      },

      async _post(url, body) {
        this.loading = true; this.error = ''; this.hint = '';
        try {
          return await ZY.api.post(url, body);
        } finally { this.loading = false; }
      },

      async sendCode() {
        if (this.loading || this.cooldown > 0) return;
        if (!this.student_no || !this.email) { this.error = '请填写学号与校园邮箱'; return; }
        try {
          const data = await this._post('/api/auth/email/code',
            {student_no: this.student_no, email: this.email});
          this.step = Math.max(this.step, 2);
          this._startCooldown();
          // 开发期假通道：接口回显验证码便于联调（email_code_echo，生产关闭）
          this.hint = data.dev_code
            ? ('开发模式验证码：' + data.dev_code + '（10 分钟内有效）')
            : '验证码已发送到邮箱，10 分钟内有效';
        } catch (e) { this.error = e.message; }
      },

      async verify() {
        const body = {student_no: this.student_no, email: this.email, code: this.code};
        if (this.step === 3) {
          Object.assign(body, {
            real_name: this.real_name, nickname: this.nickname,
            grade: this.grade || null,
            major_id: this.major_id ? Number(this.major_id) : null,
          });
        }
        try {
          const data = await this._post('/api/auth/email/verify', body);
          if (data.need_profile) {
            this.step = 3;
            this.hint = '该学号首次登录，请补全资料完成建档';
            if (!this.majors.length) {
              const mj = await ZY.api.get('/api/majors');
              this.majors = mj.items || [];
            }
            return;
          }
          window.location.href = '/';
        } catch (e) { this.error = e.message; }
      },

      reset() { this.step = 1; this.code = ''; this.error = ''; this.hint = ''; },
    };
  });
});
