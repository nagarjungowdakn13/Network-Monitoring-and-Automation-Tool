# Production Architecture

Netmon keeps the original single-process async design, but the runtime is now
split into explicit production concerns: collection, analysis, alert lifecycle,
state management, configuration and presentation.

## Module Design

- `netmon.monitor` collects psutil network counters and connection state with
  retry guards around psutil failures.
- `netmon.analyzer` applies smoothing, threshold checks, z-score spike
  detection and persistence filtering before an anomaly is emitted.
- `netmon.alerts` owns alert lifecycle, cooldown deduplication and the handler
  plugin registry.
- `netmon.state` stores live snapshots, time-series history, anomaly frequency,
  alert stats and the current system state.
- `netmon.config` loads typed configuration from YAML and environment
  overrides.
- `netmon.runner` coordinates the async loop, dynamic config reloads, state
  updates and dashboard event publishing.
- `netmon.cli` exposes the stable user commands.

This keeps compatibility with `python -m netmon ...` while allowing production
extensions without rebuilding the core.

## System State Model

The state manager tracks:

- `normal`: no active anomalies in the latest tick
- `warning`: at least one warning anomaly is active
- `critical`: at least one critical anomaly is active

Every transition is appended to `state_transitions` in the snapshot, along with
the current health payload.

## Anomaly Detection Flow

1. `NetworkMonitor.sample()` returns bandwidth, packet and connection metrics.
2. `Analyzer` smooths each metric with a moving average window.
3. Hard thresholds and rolling z-score checks produce candidate anomalies.
4. Persistence filtering requires the same anomaly key to appear for the
   configured number of intervals before emission.
5. Emitted anomalies update system state, history and anomaly frequency.
6. `AlertManager` dispatches only non-deduplicated alerts.

## Alert Lifecycle

1. An anomaly arrives with a stable key such as `threshold:mbps_in`.
2. The alert manager checks the cooldown map.
3. If the key fired recently, the event is suppressed and counted.
4. Otherwise the alert is marked fired and sent to all configured handlers.
5. Handlers run concurrently; handler errors are logged and do not crash the
   monitoring loop.

## Plugin Handlers

Built-in handler types are `log`, `console`, `script` and `webhook`.

External handlers can be added in two ways:

```python
from netmon.alerts import register_handler

async def send_to_queue(anomaly):
    ...

register_handler("queue", lambda options: send_to_queue)
```

Or by referencing a factory directly in config:

```yaml
alerts:
  handlers:
    - type: "my_package.netmon_plugins:make_queue_handler"
      queue: network-alerts
```

The factory receives the handler options and returns an async callable that
accepts an `Anomaly`.

## Dynamic Configuration

When the monitor is started with a YAML config file, the runner watches that
file's modification time. Changes to monitor, threshold, alert or state
settings are applied on the next tick without restarting the process.

Environment overrides remain supported for log level, interval and state file.
