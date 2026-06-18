# Standalone DigitalOcean Managed Postgres cluster for the saorsa-testnet-registry
# database. COMPLETELY independent of the App-Platform-attached DB defined in
# app.yaml (dev-db-722203) — this module never references it. Local tools and
# collaborators point DATABASE_URL at the cluster created here.
#
# No firewall (trusted-sources) block: access is open from any IP, gated by the
# generated user password over TLS (the emitted DATABASE_URL pins sslmode=require).
# This is deliberate so collaborators on varying/dynamic IPs can connect without
# maintaining an allow-list. To lock it down later, add a
# digitalocean_database_firewall resource.
resource "digitalocean_database_cluster" "registry" {
  name       = var.name
  engine     = "pg"
  version    = var.pg_version
  size       = var.size
  region     = var.region
  node_count = var.node_count
}

resource "digitalocean_database_db" "registry" {
  cluster_id = digitalocean_database_cluster.registry.id
  name       = var.db_name
}

resource "digitalocean_database_user" "registry" {
  cluster_id = digitalocean_database_cluster.registry.id
  name       = var.db_user
}
