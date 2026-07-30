# Web App Setup — Garmin Viewer

Written 2026-07-30. Concrete deployment/design spec for Goal 4 in `GAME_PLAN.md`. **Do not start this before Goals 2-3 (activities/workouts data, Plotly migration) are far enough along to have real data/charts to serve** — this doc exists so the design is settled and ready, not to pull the work forward.

**Pattern: mirror the `kaya` viewer exactly** — same infra shape, same visual design, same integration model, already proven end-to-end. The same spec was also written for `ticket_price_tracker` (`WEB_APP_SETUP.md` in that repo) — all three portfolio apps should end up structurally identical for consistency and so any one of them is easy to reason about once you've seen the others.

## This Replaces the Current Integration, It Doesn't Add To It

Garmin is currently embedded in `aws_flask_site` as a git submodule (`garmin/`, pinned to `prod` branch) with its Flask blueprint registered at `/garmin` via `app/integrations/project_surfaces.py`. **The kaya-style pattern below replaces this entirely** — an independent service on its own subdomain, iframe-embedded into the main site, not a blueprint living inside `aws_flask_site`'s own process. When this phase actually starts, deprecating the submodule registration is part of the work, not an optional cleanup step to defer.

## Subdomain & DNS

- Subdomain: `garmin.peterwilliams.dev`.
- DNS: proxied Cloudflare A record at the EC2 host's current public IP, via the existing `CLOUDFLARE_API_TOKEN` (zone ID `08206cb111c3f96b54ecb3db3e5ccead`) — not the dashboard.
- TLS: none needed — Flexible mode, plain HTTP on the origin side, same as every other vhost on this host.

## App Framework

FastAPI + Plotly, precomputed/static data only — no live Garmin API calls or heavy computation in the deployed app itself. The Lambda (`garmin-data-updater`) keeps writing curated data on its own schedule; this app only ever reads already-curated outputs, same "no inference in the web tier" rule as kaya and the ticket tracker.

## Deploy Target on the EC2 Host

- Directory: `/home/ubuntu/garmin_viewer`.
- **Port: `8030`.** Verify this is still free before building (`8000`=gunicorn, `8010`=kaya, `8020` reserved for the ticket tracker viewer as of this writing) — check with `sudo ss -tlnp` on the host rather than trusting this document blindly if time has passed.
- systemd unit `garmin-viewer.service`, bound to `127.0.0.1:8030` only:
  ```ini
  [Unit]
  Description=Garmin viewer
  After=network.target

  [Service]
  WorkingDirectory=/home/ubuntu/garmin_viewer
  ExecStart=/home/ubuntu/garmin_viewer/.venv/bin/uvicorn <module>:app --host 127.0.0.1 --port 8030
  Restart=on-failure
  RestartSec=5
  User=ubuntu
  Environment=GARMIN_VIEWER_ENV=production

  [Install]
  WantedBy=multi-user.target
  ```
- nginx vhost `/etc/nginx/sites-available/garmin-viewer`: `server_name garmin.peterwilliams.dev;`, plain HTTP `proxy_pass` to `127.0.0.1:8030`. Decide whether this should be auth-gated — unlike Kaya and the ticket tracker, Garmin data is personal health data, not a public dataset, so **default to gating this one** unless there's a specific reason to make it public. (This is a real difference from the other two apps — don't copy "ungated" blindly here.)

## IAM — same gotcha as every other app on this host

**Do not create a new IAM instance profile.** The host has `ec2-kaya-viewer-role`/`ec2-kaya-viewer-profile` attached already (originally scoped to kaya's S3 prefix, likely broadened again for the ticket tracker by the time this is built). **An EC2 instance can only have one instance profile at a time** — add another policy statement to the existing role for read access to `my-garmin-data`'s curated output prefix, don't create/attach a competing role. By this point the role is a shared "viewer apps" role in practice, not kaya-specific — worth considering a rename for clarity (`ec2-viewer-apps-role`) when touching it again, though not required.

## Deploy Workflow — Its Own CI, Pushed Directly From This Repo

Same pattern as kaya and the ticket tracker:
- rsync app code + dependencies to `/home/ubuntu/garmin_viewer/`.
- install/update the venv.
- `sudo systemctl restart garmin-viewer.service`.
- GitHub secrets: a host-IP secret (e.g. `GARMIN_VIEWER_HOST`) and a separate scoped deploy SSH key (e.g. `GARMIN_VIEWER_DEPLOY_KEY`) — don't share deploy keys across repos.
- **No Elastic IP on this host.** Update the host-IP secret if the instance is ever resized again — this exact gap already broke `aws_flask_site`'s deploy once.

## Integration Into the Main Site — Iframe Embed

Same as kaya's final pattern: a page on the main site (e.g. `peterwilliams.dev/garmin`, replacing the current submodule-backed route of the same path) keeps the site's nav/footer as the wrapper, with this app embedded via `<iframe src="https://garmin.peterwilliams.dev">`. If the app is gated (see above), the iframe experience needs to handle the logged-out case sensibly — check how kaya would have handled this if it had stayed gated (see `system-overview/AGENT_HANDOFF.md`'s auth-gating history) rather than re-deriving the approach from scratch, even though kaya itself ended up ungated.

## Design System — Reuse Exactly

Copy `kaya`'s theme directly: blue `#1976d2` primary / copper `#b8752e` secondary, IBM Plex Sans via `@font-face`, tight radius capped at 6px, flat/bordered surfaces, underline-style tabs. Use `kaya/src/kaya/viewer_static/tokens.css`, `design-system-reference.html`, and `site-tokens-reference.css` as the source-of-truth files, same as the ticket tracker's spec — don't re-derive the palette a third time.
