# Operator public key registered as a Contabo secret (see resources/contabo).
resource "contabo_secret" "deploy_key" {
  name  = "${lower(var.name)}-${lower(var.region)}-elastic"
  type  = "ssh"
  value = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

resource "contabo_instance" "elastic" {
  display_name = "${var.name}-ant-elastic-${var.region}"
  product_id   = var.product_id
  region       = var.region
  image_id     = var.image_id
  period       = var.period
  default_user = "root"
  ssh_keys     = [tonumber(contabo_secret.deploy_key.id)]
}

# NO data volume: Contabo has no attachable block storage. Elasticsearch data
# lives on the instance root disk (the provisioner uses a root-disk directory
# instead of mounting a block device).
