resource "digitalocean_droplet" "monitoring" {
  name     = "${var.name}-ant-monitoring-${var.region}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}

resource "digitalocean_volume" "monitoring_storage" {
  region                  = var.region
  name                    = "${lower(var.name)}-ant-monitoring-storage-${var.region}"
  size                    = var.volume_size
  initial_filesystem_type = "ext4"
}

resource "digitalocean_volume_attachment" "monitoring_storage_attach" {
  droplet_id = digitalocean_droplet.monitoring.id
  volume_id  = digitalocean_volume.monitoring_storage.id
}

resource "digitalocean_firewall" "monitoring" {
  name = "${lower(var.name)}-ant-monitoring-fw-${var.region}"

  droplet_ids = [digitalocean_droplet.monitoring.id]

  # SSH
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # Grafana
  inbound_rule {
    protocol         = "tcp"
    port_range       = "3000"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # InfluxDB
  inbound_rule {
    protocol         = "tcp"
    port_range       = "8086"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # Allow all outbound
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
