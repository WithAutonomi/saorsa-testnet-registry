resource "vultr_instance" "client_vm" {
  label            = "${var.name}-ant-client-${var.role}-${var.region}-${var.client_index}"
  hostname         = "${var.name}-ant-client-${var.role}-${var.region}-${var.client_index}"
  region           = var.region
  plan             = var.plan
  os_id            = 2284 # Ubuntu 24.04 LTS x64
  ssh_key_ids      = var.ssh_key_ids
  enable_ipv6      = true
  backups          = "disabled"
  activation_email = false
}
