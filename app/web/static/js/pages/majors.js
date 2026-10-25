(function () {
  'use strict';

  window.majorFilter = function () {
    return {
      query: '',
      cards: [],
      init() {
        this.cards = Array.from(this.$root.querySelectorAll('[data-major-card]'));
      },
      get visibleCount() {
        const query = this.query.trim().toLocaleLowerCase();
        if (!query) return this.cards.length;
        return this.cards.filter(card => (card.dataset.majorName || '').includes(query)).length;
      },
      matches(value) {
        return (value || '').includes(this.query.trim().toLocaleLowerCase());
      },
    };
  };
})();
