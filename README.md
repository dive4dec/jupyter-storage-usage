# jupyter-storage-usage

Jupyter server extension that monitors home-directory storage usage and reports it to JupyterHub. Inspired by [jupyter-resource-usage](https://github.com/jupyter-server/jupyter-resource-usage).

## What it does

This extension runs inside each user's Jupyter server pod and operates in two modes:

### Normal mode (`JSU_CLEANUP_MODE=0`, default)

- Checks disk usage of the home directory (`/home/jovyan` by default) via `du` with `nice -n 19` (lowest priority)
- Exposes usage via an API endpoint (`/api/storage/v1/usage`) for JupyterHub to poll
- Pushes usage to the hub's `/report` endpoint periodically
- Shows a **status bar widget** in JupyterLab (like `jupyter-resource-usage`)
- Uses a **persistent cache** (`.storage-usage.json` on NFS) so the widget shows the last known value instantly on pod start

### Cleanup mode (`JSU_CLEANUP_MODE=1`)

Set by the pre-spawn hook when a user is over their hard limit. In this mode:

- Takes over **all URLs** — the cleanup portal replaces JupyterLab entirely
- Serves a file manager at `/cleanup/` — browse, delete, download
- **Blocks `/lab/`, `/api/`, `/terminals/`** etc. (catch-all redirect to `/cleanup/`)
- Shows the student's **actual usage vs. their limits** with a progress bar
- Supports folder download as **zip or tar.gz**
- Shows **recursive folder sizes** so students see the real savings from deleting a folder
- Reports fresh usage to the hub **after each delete** so re-spawn works immediately
- **No upload, no create, no edit** — application-layer enforcement

## Configuration

All settings are environment variables. Defaults match the hub-side package.

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_HOME_DIR` | `/home/jovyan` | Directory to monitor |
| `JSU_WARN_LIMIT_BYTES` | `10Gi` | Soft limit — show warning |
| `JSU_HARD_LIMIT_BYTES` | `20Gi` | Hard limit — triggers cleanup mode |
| `JSU_CHECK_INTERVAL_SEC` | `1800` (30 min) | How often to run `du` locally |
| `JSU_CLEANUP_MODE` | `0` | Set to `1` by the pre-spawn hook for over-limit users |
| `JSU_HUB_REPORT_URL` | `http://hub:9099/report` | Where to report usage to the hub |

Accepts K8s-style quantities: `10Gi`, `500Mi`, `1Ti`, or plain bytes.

**Note:** The pre-spawn hook injects `JSU_WARN_LIMIT_BYTES` and `JSU_HARD_LIMIT_BYTES`
into the pod environment with the user's resolved limits (from per-user/group overrides).
The defaults here are only used when the pod is not spawned through JupyterHub.

## How it works with JupyterHub

```
JupyterHub (hub pod)                    User pod
┌─────────────────────┐                ┌──────────────────────┐
│ ServerPoller        │──poll──────────▶│ /api/storage/v1/usage│
│ (every 600s)        │                │                      │
│                     │                │ jupyter-storage-     │
│ /report endpoint    │◀──report───────│ usage extension      │
│ (receives updates)  │  (every 1800s  │                      │
│                     │   + after each │ Status bar widget    │
│ Pre-spawn hook      │   delete in    │ (normal mode)        │
│ (swaps to cleanup   │   cleanup mode)│                      │
│  image if over)     │                │ Cleanup portal       │
└─────────────────────┘                │ (cleanup mode)       │
                                       └──────────────────────┘
```

## Install

```bash
pip install jupyter-storage-usage
```

The extension auto-registers via `jupyter_server_config.d/jupyter_storage_usage.json`.

## License

MIT
