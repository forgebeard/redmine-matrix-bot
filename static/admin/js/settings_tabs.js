/**
 * Управление вкладками на странице Настройки.
 * Сохраняет активную вкладку в URL (#general / #dictionary / #notifications).
 */
(function () {
  var tabsContainer = document.getElementById('settings-tabs');
  if (!tabsContainer) return;

  var btns = tabsContainer.querySelectorAll('.tab-btn');
  var contents = document.querySelectorAll('.tab-content');

  function showTab(tabId) {
    btns.forEach(function (btn) {
      var isActive = btn.getAttribute('data-tab') === tabId;
      btn.classList.toggle('active', isActive);
    });
    contents.forEach(function (c) {
      c.classList.toggle('active', c.id === 'tab-' + tabId);
    });
    window.location.hash = tabId;
  }

  btns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      showTab(this.getAttribute('data-tab'));
    });
  });

  // Restore from URL
  var hash = window.location.hash.replace('#', '');
  var allowedTabs = ['general', 'dictionary', 'notifications'];
  if (allowedTabs.indexOf(hash) >= 0) {
    showTab(hash);
  }
})();
