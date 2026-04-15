resource "vultr_instance" "node_vm" {
  count       = var.vm_count
  label       = "${var.name}-ant-node-${var.region}-${count.index + 1}"
  hostname    = "${var.name}-ant-node-${var.region}-${count.index + 1}"
  region      = var.region
  plan        = "vc2-2c-4gb"
  os_id       = 2284 # Ubuntu 24.04 LTS x64
  ssh_key_ids = var.ssh_key_ids
  enable_ipv6 = true
  backups     = "disabled"
  activation_email = false
}

resource "vultr_block_storage" "node_storage" {
  count                = var.vm_count
  label                = "${lower(var.name)}-ant-storage-${var.region}-${count.index + 1}"
  region               = var.region
  size_gb              = var.attached_volume_size
  attached_to_instance = vultr_instance.node_vm[count.index].id
  live                 = true
}
