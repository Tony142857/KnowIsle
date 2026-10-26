/* Motion One 按需加载与统一降级；业务代码不直接依赖全局 Motion。 */
window.ZY = window.ZY || {};
(function () {
  'use strict';
  var loading = null;
  function reduced() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  function load() {
    if (reduced()) return Promise.resolve(false);
    if (window.Motion) return Promise.resolve(true);
    if (!loading) {
      loading = new Promise(function (resolve, reject) {
        var script = document.createElement('script');
        script.src = '/static/vendor/motion-10.18.0.min.js';
        script.async = true;
        script.onload = function () { resolve(true); };
        script.onerror = function () { reject(new Error('Motion One 加载失败')); };
        document.head.appendChild(script);
      }).catch(function () {
        loading = null;
        return false;
      });
    }
    return loading;
  }
  function animate(el, keyframes, options) {
    if (!el || reduced()) return Promise.resolve(false);
    return load().then(function (ready) {
      if (!ready || !el.isConnected) return false;
      var config = Object.assign({duration: 0.2, ease: 'easeOut'}, options || {});
      window.Motion.animate(el, keyframes, config);
      return true;
    });
  }
  window.ZY.motion = {
    load: load,
    reduced: reduced,
    enter: function (el) {
      return animate(el, {opacity: [0, 1], transform: ['translateY(6px)', 'translateY(0px)']});
    },
    tabs: function (el) {
      return animate(el, {opacity: [0.65, 1]}, {duration: 0.16});
    },
    update: function (el) {
      return animate(el, {opacity: [0.55, 1]}, {duration: 0.18});
    },
    highlight: function (el) {
      if (!el || reduced()) return Promise.resolve(false);
      return animate(el, {
        boxShadow: ['0 0 0 0 rgba(221,148,36,0)', '0 0 0 4px rgba(221,148,36,0.22)', '0 0 0 0 rgba(221,148,36,0)'],
      }, {duration: 0.24});
    },
  };
})();
