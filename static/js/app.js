// Native dialogs and field hints share keyboard and positioning rules.
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => {
    if (event.target === dialog) {
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    }
  });
});
const navigation = document.getElementById('app-navigation');
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && navigation.classList.contains('show') && window.innerWidth < 992) {
    tabler.Collapse.getOrCreateInstance(navigation).hide();
    document.querySelector('[aria-controls="app-navigation"]').focus();
  }
});
