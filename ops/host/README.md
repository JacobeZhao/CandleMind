# Host Administration

`setup_docker_mirror.sh` creates a Docker daemon configuration only when the
target file does not already exist. It is an optional China-network bootstrap
helper, not part of CandleMind startup.

Run it only after reviewing the target host and desired mirrors:

```sh
sudo sh ops/host/setup_docker_mirror.sh /etc/docker/daemon.json
```

The script deliberately does not signal or restart Docker. Validate the JSON
and use the host's service manager to reload Docker.
