/* 首页动态流（§13）：GET /api/feed → {new_resources: [...], hot_posts: [...]}
 * 匿名调用同样返回 200（new_resources 恒为空），两种登录态共用一条加载路径。
 * 原 index.html 内联脚本外置（CSP 兼容）。
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
      fmt(iso) { return (iso || '').slice(0, 10); },
      async init() {
        this.loading = true; this.error = '';
        try {
          const data = await ZY.api.get('/api/feed');
          this.newResources = data.new_resources || [];
          this.hotPosts = data.hot_posts || [];
        } catch (e) { this.error = e.message || '动态加载失败'; }
        finally { this.loading = false; }
      },
    };
  });
});
