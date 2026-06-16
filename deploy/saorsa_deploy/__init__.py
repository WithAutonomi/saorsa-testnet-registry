"""saorsa-deploy package.

Importing the package applies two process-wide SSH connector defaults:

* connect-retry defaults so that transient connection failures during
  provisioning — most commonly "Error reading SSH protocol banner" from a
  freshly-booted droplet whose sshd is still settling — are retried
  automatically instead of failing a whole deploy step. See
  :func:`saorsa_deploy.ssh.configure_ssh_connect_retries`.
* a ``/dev/null`` known_hosts default so a recycled cloud IP carrying a stale
  host key can't raise ``BadHostKeyException`` and fail a provision step (and
  so provisioning stops polluting the user's ``~/.ssh/known_hosts``). See
  :func:`saorsa_deploy.ssh.configure_ssh_known_hosts`.
"""

from saorsa_deploy.ssh import (
    configure_ssh_connect_retries,
    configure_ssh_known_hosts,
)

configure_ssh_connect_retries()
configure_ssh_known_hosts()
