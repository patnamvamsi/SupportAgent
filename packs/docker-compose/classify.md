How symptoms map to labels for components run as containers:
- runtime/down: the component is not running. Its container exited, was never started after a restart or reboot, or keeps restarting; requests to it are refused or time out, and what depends on it fails.
- runtime/stalled: the component is running (its container reports up, health checks may pass) but its work has stopped: no new output, no new data where it writes, consumers see nothing new. Nothing is erroring loudly.
- A slow but working component is a latency label, not runtime/*. Errors on every request from a running component are an app/* label.
