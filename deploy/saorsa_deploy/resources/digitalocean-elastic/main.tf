resource "digitalocean_droplet" "elastic" {
  name     = "${var.name}-ant-elastic-${var.region}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}

resource "digitalocean_volume" "elastic_storage" {
  region                  = var.region
  name                    = "${lower(var.name)}-ant-elastic-storage-${var.region}"
  size                    = var.volume_size
  initial_filesystem_type = "ext4"
}

resource "digitalocean_volume_attachment" "elastic_storage_attach" {
  droplet_id = digitalocean_droplet.elastic.id
  volume_id  = digitalocean_volume.elastic_storage.id
}

# A reserved IP keeps one address across droplet rebuilds, so the public DNS
# record for a permanent instance is set once and never again. Only created for
# a public-ingest instance that was not handed an existing address to adopt —
# a testnet's ephemeral stack has no DNS to protect and creates nothing.
resource "digitalocean_reserved_ip" "elastic" {
  count  = var.public_ingest && var.reserved_ip == "" ? 1 : 0
  region = var.region
}

resource "digitalocean_reserved_ip_assignment" "elastic" {
  count      = var.public_ingest ? 1 : 0
  ip_address = var.reserved_ip != "" ? var.reserved_ip : digitalocean_reserved_ip.elastic[0].ip_address
  droplet_id = digitalocean_droplet.elastic.id

  # DigitalOcean serialises actions on a droplet and rejects a second one with
  # 422 "Droplet already has a pending event". Attaching the volume and
  # assigning the reserved IP are both droplet actions, and Terraform has no
  # reason to order them — it starts both at once and one loses the race.
  # Observed on the first real deploy. Ordering them is the fix; there is no
  # retry to rely on.
  depends_on = [digitalocean_volume_attachment.elastic_storage_attach]
}

resource "digitalocean_firewall" "elastic" {
  name = "${lower(var.name)}-ant-elastic-fw-${var.region}"

  droplet_ids = [digitalocean_droplet.elastic.id]

  # SSH
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # Elasticsearch, exposed directly. Only for the private (testnet) shape: a
  # public-ingest instance binds ES to localhost and is reached exclusively
  # through the TLS proxy below, so leaving 9200 open would be an unauthenticated
  # plaintext path straight past it.
  dynamic "inbound_rule" {
    for_each = var.public_ingest ? [] : [1]
    content {
      protocol         = "tcp"
      port_range       = "9200"
      source_addresses = ["0.0.0.0/0", "::/0"]
    }
  }

  # HTTPS ingest. Port 80 is deliberately NOT opened: the certificate is issued
  # over TLS-ALPN-01, which runs on 443, so there is no HTTP-01 challenge to
  # serve and no cleartext listener to redirect from.
  dynamic "inbound_rule" {
    for_each = var.public_ingest ? [1] : []
    content {
      protocol         = "tcp"
      port_range       = "443"
      source_addresses = ["0.0.0.0/0", "::/0"]
    }
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
