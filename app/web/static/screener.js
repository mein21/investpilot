(function () {
  const search = document.getElementById("candidate-search");
  const sort = document.getElementById("candidate-sort");
  const visible = document.getElementById("candidate-visible");
  const tbody = document.getElementById("candidate-rows");
  const grid = document.getElementById("candidate-cards");
  if (!search || !sort || !visible || !tbody || !grid) return;
  const rows = Array.from(tbody.querySelectorAll("tr[data-candidate]"));
  const cards = Array.from(grid.querySelectorAll("[data-candidate]"));
  const total = rows.length;
  function value(node, key) {
    const raw = node.dataset[key];
    if (raw === undefined || raw === "") return null;
    const number = Number(raw);
    return Number.isFinite(number) ? number : raw.toLowerCase();
  }
  function apply() {
    const query = search.value.trim().toLowerCase();
    const parts = sort.value.split("-");
    const key = parts[0];
    const direction = parts[1];
    const matches = (node) => {
      if (!query) return true;
      const haystack = node.dataset.ticker + " " + node.dataset.name + " " + node.dataset.sector;
      return haystack.includes(query);
    };
    const visibleRows = rows.filter(matches);
    const visibleCards = cards.filter(matches);
    rows.forEach((node) => { node.hidden = !matches(node); });
    cards.forEach((node) => { node.hidden = !matches(node); });
    const compare = (left, right) => {
      const a = value(left, key);
      const b = value(right, key);
      if (a === b) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      const result = a < b ? -1 : 1;
      return direction === "asc" ? result : -result;
    };
    visibleRows.sort(compare).forEach((node, index) => {
      tbody.appendChild(node);
      const rank = node.querySelector(".candidate-rank");
      if (rank) rank.textContent = (index + 1) + " / " + visibleRows.length;
    });
    visibleCards.sort(compare).forEach((node, index) => {
      grid.appendChild(node);
      const rank = node.querySelector(".candidate-rank");
      if (rank) rank.textContent = "#" + (index + 1) + " из " + visibleCards.length;
    });
    visible.textContent = visibleRows.length + " из " + total + " кандидатов";
  }
  search.addEventListener("input", apply);
  sort.addEventListener("change", apply);
  apply();
})();
