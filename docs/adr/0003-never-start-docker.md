# 3. Never wake a stopped Docker daemon

**Status:** Accepted  
**Date:** 2026-10-19

## Context

Docker is often socket-activated: touching the socket starts the daemon. Just opening a panel shouldn't start a service.

## Decision

patra checks whether the daemon is running before asking it anything; when it isn't, it says so and offers to start it.

## Consequences

- Opening the panel is free.
- Starting Docker is an explicit action.
