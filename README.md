# Scotty deployment

A minimal, deterministic package that prepares a stopped Scotty gateway container from a pinned official Nous Research image. It is designed for Debian 13 with Docker 26.1.5, Compose 2.26.1, iptables 1.8.11, systemd, AppArmor, seccomp, and private cgroup namespaces.

## Security boundary

The installer creates `/srv/Scotty/data` as the only container mount and keeps root-owned Compose/operator material outside that mount. The container has no published ports, Docker or Podman socket, devices, or privileged mode. It uses `no-new-privileges`, 2 CPUs, 4 GiB memory, 512 PIDs, no Docker restart policy, and the external `scotty-egress` bridge (`172.30.50.0/24`). A persistent, first-priority `DOCKER-USER` guard rejects host, private, Tailnet, metadata/link-local, multicast, documentation, benchmarking, and reserved IPv4 destinations while permitting public-internet egress.

The official image initializes under root through s6, then drops supervised gateway processes to UID/GID 10000. There is intentionally no Compose `user` override because it would bypass required image initialization.

A folder name, prompt, or persona is not a security boundary. The enforced boundary is the container configuration plus host firewall, and credentials still determine the agent's external authority.

## Verify

```sh
make test
```

## Install

Run exactly once from a clean checkout:

```sh
sudo ./install.sh
```

The installer performs all preflight checks before mutation, then creates the root-owned operator files, active firewall guard, bridge, and container. It never starts the container. Any error, interrupt, termination, or uncommitted exit triggers in-process cleanup limited to objects created by that invocation. There is no persistent rollback or uninstall artifact.

The install fails closed if `/srv/Scotty`, the container, bridge, firewall chain/jump, installed operator files, or systemd unit already exists.

## Credentials and activation

Credential entry and activation are intentionally separate, later phases. This repository contains no credential file, environment file, setup-wizard automation, start command, exposed port, or service activation for the gateway. Do not start the container or run the setup wizard until a separately reviewed credential and operating-policy phase is approved.
