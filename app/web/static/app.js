(() => {
  const chartDefaults = {
    borderColor: "#f5f8f2",
    borderWidth: 3,
    hoverOffset: 5,
  };

  const palette = ["#1c6a62", "#10211d", "#df522c", "#b88a2e", "#9fb2a7", "#687a71"];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const motion = reduceMotion ? false : { duration: 720, easing: "easeOutQuart" };

  function renderAllocationChart() {
    const canvas = document.getElementById("allocation-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const raw = canvas.getAttribute("data-chart") || "[]";
    let rows = [];
    try {
      rows = JSON.parse(raw);
    } catch {
      return;
    }
    const labels = rows.map((row) => row.label);
    const values = rows.map((row) => Number(row.value));
    new Chart(canvas, {
      type: "doughnut",
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: palette.slice(0, values.length),
          ...chartDefaults,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "73%",
        animation: reduceMotion ? false : {
          animateRotate: true,
          animateScale: true,
          duration: 820,
          easing: "easeOutQuart",
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label(context) {
                return ` ${context.label}: ${Number(context.raw).toFixed(2).replace(".", ",")}%`;
              },
            },
          },
        },
      },
    });
  }

  function animateBar(element, property, value) {
    element.style[property] = `${value}%`;
    if (reduceMotion || typeof element.animate !== "function") return;
    element.getAnimations().forEach((animation) => animation.cancel());
    element.animate(
      [{ [property]: "0%" }, { [property]: `${value}%` }],
      {
        duration: 820,
        delay: 120,
        easing: "cubic-bezier(0.16, 1, 0.3, 1)",
        fill: "both",
      },
    );
  }

  function applyBarWidths() {
    document.querySelectorAll("[data-width]").forEach((element) => {
      const value = Math.max(0, Math.min(100, Number(element.getAttribute("data-width"))));
      animateBar(element, "width", Number.isFinite(value) ? value : 0);
    });
    document.querySelectorAll("[data-left]").forEach((element) => {
      const value = Math.max(0, Math.min(100, Number(element.getAttribute("data-left"))));
      animateBar(element, "left", Number.isFinite(value) ? value : 0);
    });
  }

  function initCountUps() {
    if (reduceMotion) return;
    document.querySelectorAll("[data-countup]").forEach((element) => {
      const target = Number(element.getAttribute("data-countup"));
      const currency = element.getAttribute("data-countup-currency");
      if (!Number.isFinite(target) || !currency) return;
      const formatter = new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
      const startValue = target * 0.9;
      const duration = 620;
      const started = performance.now();
      const frame = (now) => {
        const progress = Math.min(1, (now - started) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        element.textContent = formatter.format(startValue + (target - startValue) * eased);
        if (progress < 1) requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    });
  }

  function initTopdeck() {
    const topdeck = document.querySelector(".topdeck");
    if (!topdeck) return;
    const update = () => topdeck.classList.toggle("is-scrolled", window.scrollY > 8);
    update();
    window.addEventListener("scroll", update, { passive: true });
  }

  function renderEquityChart() {
    const canvas = document.getElementById("equity-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const raw = canvas.getAttribute("data-equity") || "[]";
    let rows = [];
    try {
      rows = JSON.parse(raw);
    } catch {
      return;
    }
    if (!rows.length) return;
    const labels = rows.map((row) => row.label);
    const values = rows.map((row) => Number(row.value));
    const context = canvas.getContext("2d");
    const gradient = context.createLinearGradient(0, 0, 0, 230);
    gradient.addColorStop(0, "rgba(223, 82, 44, 0.2)");
    gradient.addColorStop(1, "rgba(223, 82, 44, 0.01)");
    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: "#df522c",
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: values.length > 20 ? 0 : 2,
          pointBackgroundColor: "#df522c",
          fill: true,
          tension: 0.28,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: motion,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label(context) {
                const value = Number(context.raw);
                return ` ${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(value)} ₽`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: "#5d7168", maxTicksLimit: 8, font: { size: 9, family: "JetBrains Mono" } },
            border: { color: "rgba(16,33,29,0.16)" },
          },
          y: {
            grid: { color: "rgba(16,33,29,0.08)" },
            ticks: {
              color: "#5d7168",
              font: { size: 9, family: "JetBrains Mono" },
              callback(value) {
                const number = Number(value);
                if (Math.abs(number) >= 1000000) return `${(number / 1000000).toFixed(2)} млн`;
                if (Math.abs(number) >= 1000) return `${Math.round(number / 1000)} тыс.`;
                return number;
              },
            },
            border: { display: false },
          },
        },
      },
    });
  }

  function renderBacktestChart() {
    const canvas = document.getElementById("backtest-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const raw = canvas.getAttribute("data-backtest") || "[]";
    let rows = [];
    try {
      rows = JSON.parse(raw);
    } catch {
      return;
    }
    if (!rows.length) return;
    const labels = rows.map((row) => row.label);
    const values = rows.map((row) => Number(row.value));
    const context = canvas.getContext("2d");
    const gradient = context.createLinearGradient(0, 0, 0, 230);
    gradient.addColorStop(0, "rgba(28, 106, 98, 0.2)");
    gradient.addColorStop(1, "rgba(28, 106, 98, 0.01)");
    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: "#1c6a62",
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: values.length > 20 ? 0 : 2,
          pointBackgroundColor: "#1c6a62",
          fill: true,
          tension: 0.28,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: motion,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label(context) {
                const value = Number(context.raw);
                return ` ${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(value)} ₽`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: "#5d7168", maxTicksLimit: 8, font: { size: 9, family: "JetBrains Mono" } },
            border: { color: "rgba(16,33,29,0.16)" },
          },
          y: {
            grid: { color: "rgba(16,33,29,0.08)" },
            ticks: {
              color: "#5d7168",
              font: { size: 9, family: "JetBrains Mono" },
              callback(value) {
                const number = Number(value);
                if (Math.abs(number) >= 1000000) return `${(number / 1000000).toFixed(2)} млн`;
                if (Math.abs(number) >= 1000) return `${Math.round(number / 1000)} тыс.`;
                return number;
              },
            },
            border: { display: false },
          },
        },
      },
    });
  }

  function init() {
    initTopdeck();
    initCountUps();
    applyBarWidths();
    renderAllocationChart();
    renderEquityChart();
    renderBacktestChart();
    const toggle = document.querySelector("[data-nav-toggle]");
    const nav = document.querySelector(".nav");
    if (toggle && nav) {
      toggle.addEventListener("click", () => {
        const open = nav.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", String(open));
      });
      document.addEventListener("click", (event) => {
        if (!nav.contains(event.target) && !toggle.contains(event.target)) {
          nav.classList.remove("is-open");
          toggle.setAttribute("aria-expanded", "false");
        }
      });
    }
    document.querySelectorAll("[data-account-switch]").forEach((form) => {
      form.querySelector("select")?.addEventListener("change", () => form.submit());
    });
    document.querySelectorAll("[data-dismiss]").forEach((element) => {
      element.addEventListener("click", () => {
        const flash = element.closest(".flash");
        if (!flash) return;
        flash.classList.add("is-leaving");
        window.setTimeout(() => flash.remove(), 180);
      });
    });
    document.querySelectorAll("[data-confirm]").forEach((element) => {
      element.addEventListener("click", (event) => {
        const message = element.getAttribute("data-confirm");
        if (message && !window.confirm(message)) {
          event.preventDefault();
          event.stopPropagation();
        }
      });
    });
    document.body.addEventListener("htmx:responseError", () => {
      window.location.reload();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
