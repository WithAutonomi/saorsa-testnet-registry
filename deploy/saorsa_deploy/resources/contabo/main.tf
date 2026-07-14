# Register the operator's public key as a Contabo "secret" (their SSH-key store).
# The instance references it by numeric secretId. Note contabo_secret.id is a
# STRING but contabo_instance.ssh_keys wants a list(number), hence tonumber().
resource "contabo_secret" "deploy_key" {
  name  = "${lower(var.name)}-${lower(var.region)}-node"
  type  = "ssh"
  value = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

resource "contabo_instance" "node_vm" {
  count        = var.vm_count
  display_name = "${var.name}-ant-node-${var.region}-${count.index + 1}"
  product_id   = var.product_id
  region       = var.region
  image_id     = var.image_id
  period       = var.period

  # Authorize the deploy key for root so the downstream provisioner connects as
  # root uniformly across providers (Contabo injects the key for default_user).
  default_user = "root"
  ssh_keys     = [tonumber(contabo_secret.deploy_key.id)]
}

# NO attached data volume: Contabo's Terraform provider has no standalone
# attachable block-storage resource (extra disk is only an in-contract
# `extraStorage` add-on). These nodes store data on the instance root disk, so
# the provisioner uses a root-disk /mnt/ant-node-storage directory rather than
# mounting a block device (same model as the ovh_3az provider).
