"""saorsa-deploy package.

Importing the package applies process-wide SSH connect-retry defaults so that
transient connection failures during provisioning — most commonly "Error
reading SSH protocol banner" from a freshly-booted droplet whose sshd is still
settling — are retried automatically instead of failing a whole deploy step.
See :func:`saorsa_deploy.ssh.configure_ssh_connect_retries`.
"""

from saorsa_deploy.ssh import configure_ssh_connect_retries

configure_ssh_connect_retries()
