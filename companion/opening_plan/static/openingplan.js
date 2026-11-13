// Opening plan (companion/opening_plan): adds one header link that opens the plan screen in its own
// window. Loaded by the dashboard only when the server lists "openingplan" in its features, which it
// does only when EDGE_SCANNER_URL is set (or in --demo). The screen is display only: no orders.
(() => {
  const slot = document.querySelector("header span.only-main");
  if (!slot || document.getElementById("op-open")) return;
  const a = document.createElement("a");
  a.id = "op-open";
  a.className = "btn sm ghost";
  a.href = "/opening-plan";
  a.target = "opening-plan";
  a.rel = "noopener";
  a.textContent = "Opening plan ⧉";
  a.title = "OV picks opening plan: break levels, stops and shares from Edge Scanner. Display only, no orders.";
  a.style.textDecoration = "none";
  a.style.display = "inline-flex";
  a.style.alignItems = "center";
  slot.insertBefore(a, slot.firstChild);
})();
