/* 首页交互（§13，v1.1 重设计）：
 * - homeFeed：动态流 GET /api/feed → {new_resources: [...], hot_posts: [...]}
 *   匿名调用同样返回 200（new_resources 恒为空），两种登录态共用一条加载路径；
 *   列表时间统一走 ZY.relTime 相对格式（components.js）。
 * - countUp：Hero 平台统计数字滚动动画。进入视口（IntersectionObserver）触发一次，
 *   easeOutCubic 缓动；prefers-reduced-motion 或不支持 IO 时直接显示终值。
 *
 * 加载时序：与 pages/login.js 相同——本文件以非 defer 方式在 head 解析期执行，
 * 早于 defer 的 Alpine 启动，经 alpine:init 注册组件。
 */
document.addEventListener('alpine:init', function () {
  'use strict';

  Alpine.data('homeFeed', function () {
    return {
      loading: true, error: '',
      newResources: [], hotPosts: [],
      rel(iso) {
        return window.ZY && ZY.relTime ? ZY.relTime(iso) : (iso || '').slice(0, 10);
      },
      async init() {
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.get('/api/feed');
          this.newResources = data.new_resources || [];
          this.hotPosts = data.hot_posts || [];
          this.$nextTick(() => { if (window.ZY.motion) ZY.motion.update(this.$el); });
        } catch (e) { this.error = e.message || '动态加载失败'; }
        finally { this.loading = false; }
      },
    };
  });

  Alpine.data('countUp', function (target) {
    return {
      target: Math.max(0, parseInt(target, 10) || 0),
      display: 0,
      _started: false,
      init() {
        var reduced = window.matchMedia &&
          window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (reduced || typeof IntersectionObserver === 'undefined') {
          this.display = this.target;
          return;
        }
        var self = this;
        var io = new IntersectionObserver(function (entries) {
          for (var i = 0; i < entries.length; i++) {
            if (entries[i].isIntersecting) {
              io.disconnect();
              self._run();
              break;
            }
          }
        }, {threshold: 0.35});
        io.observe(this.$el);
      },
      _run() {
        if (this._started) return;
        this._started = true;
        var self = this;
        var start = null;
        var DURATION = 250;
        function step(ts) {
          if (start === null) start = ts;
          var p = Math.min(1, (ts - start) / DURATION);
          var eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
          self.display = Math.round(self.target * eased);
          if (p < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
      },
    };
  });
});
