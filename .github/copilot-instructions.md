# Copilot Instructions For garmin

See `CLAUDE.md` (architecture, run/validate commands, coding rules) and `GARMIN_HANDOFF.md` (AWS resources, CLI profiles, ops commands) at repo root — this file just adds the couple of things specific to Copilot/GitHub workflows.

## Branch workflow

- Default stable branch: `main`
- Integration branch: `dev`
- Production branch: `prod`
- Tag release-ready commits on `main` as `vX.Y.Z`; `prod` should reflect deployable releases only.

## Deployment

- `.github/workflows/deploy-ec2.yml` deploys the repo to EC2 on `prod`.
- `.github/workflows/deploy-lambda.yml` deploys the Lambda updater on `prod`.

## V2 / website coordination

- This repo is the source of truth for Garmin logic even though `aws_flask_site` still carries an embedded copy (git submodule, registered at `/garmin`) — see `GAME_PLAN.md` Goal 4 for the plan to replace that with a standalone subdomain app.
- If a change affects how the website integrates Garmin outputs, call out the required update in `aws_flask_site` rather than assuming both repos are open.
- Don't edit generated dashboard HTML or `data/` artifacts unless the task is explicitly about regenerating them.

## Coordination With `system-overview`

- `system-overview` (`/Users/peterwilliams/projects/system-overview`) is the cross-repo coordination hub — its docs (`repo-inventory.md`, `system-overview.md`, `AGENT_HANDOFF.md`) are the source of truth for how this repo fits into the overall workspace (deploy targets, ports, IAM, integration mode, priorities).
- After any change here with cross-repo relevance — new deploy target, new port/subdomain, new AWS resource, new integration mode, a significant scope or status change — report it back so `system-overview` can be updated. If you can, make the edit directly in the relevant `system-overview` doc(s); otherwise leave the user a short note of what changed so they can relay it.
- Before starting work that could plausibly conflict with what `system-overview` has documented for this repo (a different port/subdomain than assigned, a new instance profile instead of extending the shared role, a deploy pattern that diverges from the established `kaya` pattern, etc.), flag it and tell the user to check with the `system-overview` agent before proceeding, rather than assuming and continuing.
