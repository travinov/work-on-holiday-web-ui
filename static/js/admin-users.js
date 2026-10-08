(() => {
  const search = document.getElementById('users-search');
  const table = document.getElementById('users-table');
  if (!search || !table) return;

  const normalize = value => value.toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
  const users = Array.from(table.querySelectorAll('[data-user-name]'), row => ({
    row,
    name: normalize(row.dataset.userName),
  }));
  const count = document.getElementById('users-count');
  const empty = document.getElementById('users-empty');

  const filter = () => {
    const words = normalize(search.value).split(/\s+/).filter(Boolean);
    let visible = 0;
    users.forEach(({ row, name }) => {
      row.hidden = !words.every(word => name.includes(word));
      if (!row.hidden) visible += 1;
    });
    count.textContent = `Показано ${visible} из ${users.length}`;
    empty.hidden = visible > 0;
  };

  search.addEventListener('input', filter);
  // Reapply a search value restored by the browser when returning to the list.
  window.addEventListener('pageshow', filter);
  filter();
})();
