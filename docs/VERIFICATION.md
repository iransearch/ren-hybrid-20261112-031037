# Verification — REN Hybrid v1

Date: 2026-11-11. Prepared for a new Render test service, not deployed.

## Evidence

- Python 3.12.14; test client/origin aiohttp 3.13.3.
- Actual HAProxy 3.2.26 compiled from the official 3.2 source archive with OpenSSL and threads.
- `python -m unittest discover -s tests -v`: 24 tests passed, no skipped tests in the recorded run.
- Generated default config and optional positive admission cap passed `haproxy -c`.
- Both bundled public Cloudflare Origin CA roots parse successfully and expire in 2029.
- Official image `haproxy:3.2.26-alpine3.24` manifest was retrieved successfully; Dockerfile pins its digest.

The tests cover real HTTPS certificate trust and hostname rejection, exact encoded path/query
forwarding, streaming HTTP upload/download, truncated HTTP response after idle timeout,
WebSocket text/binary and 2MiB messages, handshake timeout without bounding established
connection lifetime, repeated abrupt disconnect cleanup, default no application cap,
optional overload rejection with health bypass, runtime enable/disable, graceful reload with
an old WebSocket kept alive, deferral of another reload until the old worker drains, invalid
settings rejection, rejected reload keeping the old worker/config, recovery after lost runtime
and reload replies, and runtime persistence failure followed by reconciliation.

## Review and fixes

An independent read-only review identified four issues, all addressed and regression-tested:
1. Per-request HAProxy error logging could expose client addresses/queries: traffic logging removed.
2. Failed/unconfirmed reload could stop a healthy old worker: master result/state is now observed;
   known failed reload rolls back the config file, and uncertain reload is monitored without killing flows.
3. Lost runtime response or persistence failure could leave cached enabled state stale: dirty state
   forces observation and reconciliation even if the requested JSON returns to the cached value.
4. A fixed FD reserve starved small FD budgets: reserve now scales down with the existing OS budget.

## Practical limits

- This sandbox forbids AF_UNIX socket creation. Integration tests therefore substitute a test-only
  loopback TCP transport for the management channels. The HAProxy processes, TLS connections,
  HTTP/WebSocket streams and reloads are real. Production configuration remains owner-only Unix sockets.
- Docker is unavailable here, so the image itself has not been built or executed. Verify build/start,
  Unix-socket permissions and signal handling on the new Docker Render test service before switching traffic.
- No production load test or 15,000-connection test was performed. No throughput or memory-per-connection
  improvement is claimed until measured on comparable Render resources and the real origin.
- An unconfirmed reload with no recorded success/failure remains pending and preserves the serving worker.
  Persistent `pending_reload=true` needs operator inspection; no additional structural reload is issued.
- Process RSS is summed, not deduplicated. Prefer available cgroup memory for assessing container OOM risk.
