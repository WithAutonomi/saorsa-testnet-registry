resource "hcloud_server" "production_node" {
  name        = "ant-bootstrap-${var.location}"
  server_type = var.server_type
  location    = var.location
  image       = "ubuntu-24.04"
  ssh_keys    = [var.ssh_key_id]

  public_net {
    ipv4_enabled = true
    ipv4         = var.primary_ip_id
  }
}

resource "hcloud_volume" "stripe_a" {
  name     = "ant-bootstrap-stripe-a-${var.location}"
  size     = var.volume_size
  location = var.location
  format   = "ext4"
}

resource "hcloud_volume" "stripe_b" {
  name     = "ant-bootstrap-stripe-b-${var.location}"
  size     = var.volume_size
  location = var.location
  format   = "ext4"
}

resource "hcloud_volume_attachment" "stripe_a_attach" {
  volume_id = hcloud_volume.stripe_a.id
  server_id = hcloud_server.production_node.id
  automount = false
}

resource "hcloud_volume_attachment" "stripe_b_attach" {
  volume_id = hcloud_volume.stripe_b.id
  server_id = hcloud_server.production_node.id
  automount = false
}

resource "hcloud_firewall" "production_node" {
  name = "ant-bootstrap-${var.location}"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "10000"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "10000"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_firewall_attachment" "production_node" {
  firewall_id = hcloud_firewall.production_node.id
  server_ids  = [hcloud_server.production_node.id]
}
