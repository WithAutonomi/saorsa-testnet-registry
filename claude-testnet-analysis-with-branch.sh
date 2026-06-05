#!/usr/bin/env bash

claude \
  --dangerously-skip-permissions \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/evmlib \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/saorsa-transport \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/saorsa-core \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/ant-node \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/ant-protocol \
  --add-dir /home/chris/dev/github.com/jacderida/autonomi/ant-client
