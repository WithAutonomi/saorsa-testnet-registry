# Operator public key registered as a Contabo secret (see resources/contabo).
resource "contabo_secret" "deploy_key" {
  name  = "${lower(var.name)}-${lower(var.region)}-monitoring"
  type  = "ssh"
  value = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

resource "contabo_instance" "monitoring" {
  display_name = "${var.name}-ant-monitoring-${var.region}"
  product_id   = var.product_id
  region       = var.region
  image_id     = var.image_id
  period       = var.period
  default_user = "root"
  ssh_keys     = [tonumber(contabo_secret.deploy_key.id)]
}

# Grafana on its own small VM so panel rendering doesn't compete with InfluxDB.
# Optional — created only when enable_grafana is set (off by default via the CLI).
# No moved{} blocks: Contabo is a new provider with no pre-count legacy stacks.
resource "contabo_instance" "grafana" {
  count        = var.enable_grafana ? 1 : 0
  display_name = "${var.name}-ant-grafana-${var.region}"
  product_id   = var.grafana_product_id
  region       = var.region
  image_id     = var.image_id
  period       = var.period
  default_user = "root"
  ssh_keys     = [tonumber(contabo_secret.deploy_key.id)]
}

# NO data volume: Contabo has no attachable block storage. InfluxDB data lives on
# the instance root disk (the provisioner uses a root-disk directory instead of
# mounting a block device).
