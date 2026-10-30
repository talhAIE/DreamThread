# Render quick deployment

The repository-root render.yaml deploys TWO services: an HTTP API and a persistent background
worker. Neither process runs on your laptop. Paid Starter instances are specified for both;
review Render's displayed cost before deploying. Voice is disabled for this initial deployment.

1. Open Render Dashboard > Environment Groups > New Environment Group.
2. Name it exactly `dreamthread-runtime`. Use **Add from .env** to import the private
   `backend/.env.render` generated on the owner's computer. Never commit that file.
3. Set CORS_ORIGINS to a JSON array of the actual frontend HTTPS origin(s). Localhost entries
   are included in the private import for immediate local frontend development; replace them
   when deploying the frontend. APP_ENV is development until the real frontend origin is chosen.
4. Open https://dashboard.render.com/select-repo?type=blueprint and choose talhAIE/DreamThread.
5. Select branch `feature/phase-9-14-backend`, Blueprint path `render.yaml`, and deploy.
6. After both services are running, open the API service's provided URL followed by `/docs`
   and `/health/ready`. The worker has no public URL. Use the API service URL as the frontend
   backend URL. Do not assume its generated hostname until Render confirms it.

Neon is already migrated to 0010_audio_cleanup. Administrator and migration URLs are intentionally
excluded from runtime imports. Future migrations run as controlled setup/release operations.

The tester credentials are in private `backend/.env.tester` on the owner's computer. The account
is email-confirmed and uses normal Supabase password sign-in. Do not place its password in Git.
Creating an account does not bypass authorization or grant administrator privileges.

Directly deploying the API without the Render worker lets journal requests save, but analysis,
embedding, comparisons, Detective, exports and other queued work will not complete.
