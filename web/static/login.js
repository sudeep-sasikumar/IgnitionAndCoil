document.getElementById("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const r = await fetch("/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: document.getElementById("pw").value}),
  });
  if (r.ok) { location.href = "/"; return; }
  const j = await r.json().catch(() => ({}));
  err.textContent = j.error || "Sign-in failed";
});
