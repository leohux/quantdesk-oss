/* QuantDesk Mobile Sidebar Toggle */
(function() {
  function init() {
    // Only add on mobile
    if (window.innerWidth >= 768) return;

    var root = document.querySelector('#root > .min-h-screen, #root > div');
    if (!root) return;

    var aside = root.querySelector('aside, [class*="shrink-0"]');
    var main = root.querySelector('main, [class*="flex-1"]');
    if (!aside || !main) return;

    // Skip if already initialized
    if (document.querySelector('.mobile-menu-btn')) return;

    // Create hamburger button
    var btn = document.createElement('button');
    btn.className = 'mobile-menu-btn';
    btn.innerHTML = '☰';
    btn.setAttribute('aria-label', 'Menu');

    // Create overlay
    var overlay = document.createElement('div');
    overlay.className = 'sidebar-overlay';

    document.body.appendChild(btn);
    document.body.appendChild(overlay);

    function openSidebar() {
      aside.classList.add('sidebar-open');
      overlay.classList.add('active');
      btn.innerHTML = '✕';
    }

    function closeSidebar() {
      aside.classList.remove('sidebar-open');
      overlay.classList.remove('active');
      btn.innerHTML = '☰';
    }

    btn.addEventListener('click', function() {
      if (aside.classList.contains('sidebar-open')) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });

    overlay.addEventListener('click', closeSidebar);

    // Close sidebar when clicking a nav link
    aside.querySelectorAll('a, [role="link"]').forEach(function(link) {
      link.addEventListener('click', function() {
        setTimeout(closeSidebar, 100);
      });
    });

    // Handle resize
    window.addEventListener('resize', function() {
      if (window.innerWidth >= 768) {
        closeSidebar();
      }
    });
  }

  // Run on DOM ready and also after a short delay (for SPA rendering)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  // SPA might render later
  setTimeout(init, 500);
  setTimeout(init, 1500);
})();
