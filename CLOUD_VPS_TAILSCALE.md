# Optional private VPS deployment

If you prefer a normal Ubuntu VPS instead of Railway, use `docker-compose.cloud.yml`.

1. Install Docker on the VPS.
2. Copy this folder to the VPS.
3. Put your `.env` on the VPS only, set `DASHBOARD_AUTH_ENABLED=true`, and never commit it.
4. Create `data/` and place the backed-up database at `data/memesniper.db` if retaining history.
5. Run `docker compose -f docker-compose.cloud.yml up -d --build`.
6. Install Tailscale on the VPS and your phone, then use Tailscale Serve to proxy `http://127.0.0.1:8000`.

The Compose file intentionally binds port 8000 to localhost only. Do not change it to `0.0.0.0:8000` unless you also put a properly configured HTTPS reverse proxy/firewall in front of the application.
