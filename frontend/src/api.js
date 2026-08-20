const BASE_URL = "http://127.0.0.1:8000";

export async function fetchOpportunities() {
  const res = await fetch(`${BASE_URL}/opportunities`);
  if (!res.ok) throw new Error(`GET /opportunities failed: ${res.status}`);
  return res.json();
}

export async function triggerRefresh() {
  const res = await fetch(`${BASE_URL}/refresh`, { method: "POST" });
  if (!res.ok) throw new Error(`POST /refresh failed: ${res.status}`);
  return res.json();
}

export async function fetchJournal() {
  const res = await fetch(`${BASE_URL}/journal`);
  if (!res.ok) throw new Error(`GET /journal failed: ${res.status}`);
  return res.json();
}

export async function fetchPortfolio() {
  const res = await fetch(`${BASE_URL}/portfolio`);
  if (!res.ok) throw new Error(`GET /portfolio failed: ${res.status}`);
  return res.json();
}

export async function fetchPortfolioRisk() {
  const res = await fetch(`${BASE_URL}/portfolio/risk`);
  if (!res.ok) throw new Error(`GET /portfolio/risk failed: ${res.status}`);
  return res.json();
}

export async function fetchActionPlan() {
  const res = await fetch(`${BASE_URL}/action-plan`);
  if (!res.ok) throw new Error(`GET /action-plan failed: ${res.status}`);
  return res.json();
}

export async function importPortfolioCsv(broker, file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${BASE_URL}/portfolio/import?broker=${encodeURIComponent(broker)}`, {
    method: "POST",
    body: formData,
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || `POST /portfolio/import failed: ${res.status}`);
  return body;
}

export async function importPortfolioDocument(broker, file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${BASE_URL}/portfolio/import-document?broker=${encodeURIComponent(broker)}`, {
    method: "POST",
    body: formData,
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || `POST /portfolio/import-document failed: ${res.status}`);
  return body;
}

export async function confirmPortfolioImport(broker, holdings) {
  const res = await fetch(`${BASE_URL}/portfolio/confirm-import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ broker, holdings }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || `POST /portfolio/confirm-import failed: ${res.status}`);
  return body;
}

export async function recordManualTrade(broker, symbol, action, quantity, price) {
  const res = await fetch(`${BASE_URL}/portfolio/trade`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ broker, symbol, action, quantity, price }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || `POST /portfolio/trade failed: ${res.status}`);
  return body;
}
