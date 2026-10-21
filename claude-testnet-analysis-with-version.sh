#!/usr/bin/env bash

claude \
  --dangerously-skip-permissions \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/evmlib \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/saorsa-transport \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/saorsa-core \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/ant-node \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/ant-protocol \
  --add-dir /home/chris/dev/github.com/WithAutonomi/autonomi-release-ops/src/ant-client
