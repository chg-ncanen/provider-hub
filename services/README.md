# Services

Deployable services: REST APIs, MCP servers, agent schedulers, and other autonomous agents.

## Structure

- `team/` — Shared services deployed by the team (organized by team area: provider, pde, web)
- `user/` — Individual user services

## How to Add a Service

1. Create a directory under `team/<team-area>/` or `user/<your-name>/`
2. Include:
   - Source code (src/, main.py, etc.)
   - `Dockerfile` for containerization
   - `docker-compose.yml` (optional, for local dev)
   - `README.md` with deployment instructions
   - Any Kubernetes manifests or infrastructure files
3. Submit a PR for review

## Available Services

Check subdirectories for individual service documentation.

## Deployment

Services are deployed via CHG infrastructure using Docker containers. See individual service READMEs for deployment details.
