#!/usr/bin/env bash

claude \
  --dangerously-skip-permissions \
  --add-dir /home/chris/dev/releases/src/evmlib \
  --add-dir /home/chris/dev/releases/src/saorsa-transport \
  --add-dir /home/chris/dev/releases/src/saorsa-core \
  --add-dir /home/chris/dev/releases/src/ant-node \
  --add-dir /home/chris/dev/releases/src/ant-protocol \
  --add-dir /home/chris/dev/releases/src/ant-client
