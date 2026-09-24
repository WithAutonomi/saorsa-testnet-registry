resource "vultr_instance" "production_node" {
  label             = "ant-bootstrap-${var.region}"
  hostname          = "ant-bootstrap-${var.region}"
  region            = var.region
  plan              = "vc2-2c-4gb"
  os_id             = 2284 # Ubuntu 24.04 LTS x64
  ssh_key_ids       = [var.ssh_key_id]
  reserved_ip_id    = var.reserved_ip_id
  enable_ipv6       = true
  backups           = "disabled"
  activation_email  = false
}

resource "vultr_block_storage" "stripe_a" {
  label             = "ant-bootstrap-stripe-a-${var.region}"
  region            = var.region
  size_gb           = var.volume_size
  attached_to_instance = vultr_instance.production_node.id
  live              = true
}

resource "vultr_block_storage" "stripe_b" {
  label             = "ant-bootstrap-stripe-b-${var.region}"
  region            = var.region
  size_gb           = var.volume_size
  attached_to_instance = vultr_instance.production_node.id
  live              = true

  depends_on = [vultr_block_storage.stripe_a]
}

resource "vultr_firewall_group" "production_node" {
  description = "ant-bootstrap-${var.region}"
}

resource "vultr_firewall_rule" "ssh" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "22"
}

resource "vultr_firewall_rule" "node_udp" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "udp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "10000"
}

resource "vultr_firewall_rule" "node_tcp" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "tcp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "10000"
}

# WebRTC Direct, for browser-path clients. Pinned by `prod bootstrap upgrade`
# via ant-node's --webrtc-direct-port; unpinned the listener would move on
# every restart and no firewall rule could follow it.
resource "vultr_firewall_rule" "webrtc_udp" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "udp"
  ip_type           = "v4"
  subnet            = "0.0.0.0"
  subnet_size       = 0
  port              = "10001"
}

resource "vultr_firewall_rule" "ssh_v6" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "tcp"
  ip_type           = "v6"
  subnet            = "::"
  subnet_size       = 0
  port              = "22"
}

resource "vultr_firewall_rule" "node_udp_v6" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "udp"
  ip_type           = "v6"
  subnet            = "::"
  subnet_size       = 0
  port              = "10000"
}

resource "vultr_firewall_rule" "node_tcp_v6" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "tcp"
  ip_type           = "v6"
  subnet            = "::"
  subnet_size       = 0
  port              = "10000"
}

resource "vultr_firewall_rule" "webrtc_udp_v6" {
  firewall_group_id = vultr_firewall_group.production_node.id
  protocol          = "udp"
  ip_type           = "v6"
  subnet            = "::"
  subnet_size       = 0
  port              = "10001"
}
