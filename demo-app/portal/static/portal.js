const healthBadge = document.querySelector("[data-health-endpoint]");

if (healthBadge) {
  const description = document.querySelector("[data-health-description]");
  const lastSignal = document.querySelector("[data-health-age]");
  const allowedStates = new Set([
    "healthy",
    "warning",
    "unavailable",
    "no-data",
    "unknown",
  ]);

  const refreshHealth = async () => {
    healthBadge.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(healthBadge.dataset.healthEndpoint, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(`Health refresh failed with HTTP ${response.status}`);
      }
      const health = await response.json();
      if (
        !allowedStates.has(health.status) ||
        typeof health.label !== "string" ||
        typeof health.description !== "string" ||
        (health.age_seconds !== null &&
          (!Number.isFinite(health.age_seconds) || health.age_seconds < 0))
      ) {
        throw new Error("Health response did not match the expected format");
      }
      healthBadge.className = `health-pill health-${health.status}`;
      healthBadge.dataset.healthStatus = health.status;
      healthBadge.querySelector("[data-health-label]").textContent = health.label;
      healthBadge.title = health.description;
      description.textContent = health.description;
      lastSignal.textContent =
        health.age_seconds === null
          ? "Mesure calculée à partir des battements OpenTelemetry"
          : `Dernier battement reçu il y a ${Math.round(health.age_seconds)} s`;
    } catch {
      healthBadge.className = "health-pill health-unknown";
      healthBadge.dataset.healthStatus = "unknown";
      healthBadge.querySelector("[data-health-label]").textContent =
        "État inconnu";
      healthBadge.title =
        "Impossible d’actualiser le statut. Rechargez la fiche pour réessayer.";
      description.textContent =
        "Impossible d’actualiser le statut. Rechargez la fiche pour réessayer.";
    } finally {
      healthBadge.setAttribute("aria-busy", "false");
    }
  };

  window.setInterval(refreshHealth, 30000);
}
