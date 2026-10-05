# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The stated users are technical administrators or supervisors, interns who
develop one or more company applications, and an optional read-only audience
such as management. The primary operational job is to identify an application's
owner and health, then reach its technical signals quickly.

## Product Purpose

The local portal consolidates an application register, ownership and internship
dates, service health, and access to OpenTelemetry/Grafana signals. Success means
all registered services can be found in one place and a newly onboarded service
can be connected with a short, repeatable guide.

## Positioning

Each application record is linked to its telemetry by the OpenTelemetry
`service.name`, bringing service ownership and operational signals together in
one local interface.

## Operating Context

The system is intended for a small company and runs on a local Docker Compose
server without requiring an external service. Administrators manage accounts
and application configuration; interns manage their own application records
and consult their dashboards; readers have consultation-only access.

## Capabilities and Constraints

The stated scope includes an application list and detail, ownership, repository,
version and internship dates, application lifecycle/archival, authentication
with roles, and OpenTelemetry/Grafana observability. The target is up to 30
applications and 20 simultaneous users. The first implementation uses FastAPI
and SQLite, with Caddy as the local entry point.

Still undecided in the specification: mail-server settings and recipients,
whether the optional reader role is required at initial rollout, and final
status/latency thresholds. The user-registration flow is not specified; account
provisioning is therefore administrator-managed for this implementation.

## Evidence on Hand

The project contains the French requirements document
`Cahier des charges – Portail de suivi des applications des stagiaires.docx`.
It also contains a working local OpenTelemetry Collector, Prometheus, Tempo,
Loki, Grafana and a synthetic FastAPI application. No customer testimonials,
production application data, logos or brand assets were supplied.

## Product Principles

- Keep application ownership and technical health connected through
  `service.name`.
- Make access control explicit and enforce it on every protected action.
- Keep core workflows available on the local network without external services.
- Prefer operational facts and actionable status over decorative reporting.

## Accessibility & Inclusion

The requirements specify support for recent Chrome, Firefox and Edge browsers.
Keyboard access, visible focus, semantic forms, responsive layouts and readable
contrast are implementation expectations; no formal conformance level was
specified.
